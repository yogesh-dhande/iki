"""Sign-in, sign-out, and the allauth endpoints Iki exposes."""

from typing import Any

from allauth.account.views import AccountInactiveView
from allauth.socialaccount.providers.openid_connect import views as oidc_views
from allauth.socialaccount.providers.saml import views as saml_views
from allauth.socialaccount.views import LoginCancelledView, LoginErrorView
from django.contrib.auth import logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from iki_server.accounts.models import IdentityProvider
from iki_server.setup.models import Installation
from iki_server.web.ratelimit import RateLimiter, client_key

#: Sign-ins one client address can start per minute (per web pod).
sign_in_limiter = RateLimiter(limit_per_minute=20)


def safe_next(request: HttpRequest, default: str = "/") -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return default


@never_cache
@require_GET
def login(request: HttpRequest) -> HttpResponse:
    installation = Installation.get()
    next_url = safe_next(request)
    if request.user.is_authenticated:
        return redirect(next_url)
    provider = IdentityProvider.objects.filter(is_enabled=True).first()
    if not installation.is_set_up or provider is None:
        return render(request, "accounts/not_set_up.html", status=503)
    return render(
        request,
        "accounts/login.html",
        {
            "provider": provider,
            "sign_in_url": provider.sign_in_url(),
            "next": next_url,
        },
    )


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return redirect("signed_out")


@never_cache
@require_GET
def signed_out(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("home")
    return render(request, "accounts/signed_out.html")


def _rate_limited(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/sign_in_error.html", {"reason": "rate_limited"}, status=429)


@require_http_methods(["GET", "POST"])
def start_sign_in(request: HttpRequest, provider_id: str) -> HttpResponse:
    """allauth's OIDC login view, rate limited per client address."""
    if request.method == "POST" and not sign_in_limiter.allow(client_key(request)):
        return _rate_limited(request)
    return oidc_views.login(request, provider_id)  # type: ignore[no-any-return]


@require_http_methods(["GET", "POST"])
def start_saml_sign_in(request: HttpRequest, organization_slug: str) -> HttpResponse:
    """allauth's SAML login view, rate limited per client address."""
    if request.method == "POST" and not sign_in_limiter.allow(client_key(request)):
        return _rate_limited(request)
    return saml_views.login(request, organization_slug=organization_slug)  # type: ignore[no-any-return]


#: SAML responses one client address can post per minute (per web pod).
#: Checking a response's signature costs CPU, and anyone can post one.
acs_limiter = RateLimiter(limit_per_minute=20)


@csrf_exempt  # the identity provider posts SAML responses cross-site
@require_POST
def saml_acs(request: HttpRequest, organization_slug: str) -> HttpResponse:
    """Where the identity provider posts SAML responses (allauth's ACS), rate limited."""
    if not acs_limiter.allow(client_key(request)):
        return _rate_limited(request)
    return saml_views.acs(request, organization_slug=organization_slug)  # type: ignore[no-any-return]


def not_available(request: HttpRequest, **_: Any) -> HttpResponse:  # noqa: ANN401
    """Stands in for allauth pages Iki doesn't offer (local sign-up, account linking)."""
    return render(request, "accounts/sign_in_error.html", {"reason": "not_available"}, status=404)


#: The allauth URLs Iki needs, under /accounts/. allauth's full URLconf also
#: has password, email and account-linking pages, which SSO-only Iki doesn't
#: offer; the names it reverses internally are mapped to Iki's pages.
allauth_urlpatterns = [
    path("oidc/<str:provider_id>/login/", start_sign_in, name="openid_connect_login"),
    path(
        "oidc/<str:provider_id>/login/callback/",
        oidc_views.callback,
        name="openid_connect_callback",
    ),
    path("saml/<slug:organization_slug>/login/", start_saml_sign_in, name="saml_login"),
    path("saml/<slug:organization_slug>/acs/", saml_acs, name="saml_acs"),
    path(
        "saml/<slug:organization_slug>/acs/finish/",
        require_GET(saml_views.finish_acs),
        name="saml_finish_acs",
    ),
    path(
        "saml/<slug:organization_slug>/metadata/",
        require_GET(saml_views.metadata),
        name="saml_metadata",
    ),
    # Single logout isn't offered: signing out of Iki doesn't sign people out
    # of their identity provider (ADR 0023). The name is needed for metadata.
    path("saml/<slug:organization_slug>/sls/", not_available, name="saml_sls"),
    path("login/cancelled/", LoginCancelledView.as_view(), name="socialaccount_login_cancelled"),
    path("login/error/", LoginErrorView.as_view(), name="socialaccount_login_error"),
    path("inactive/", AccountInactiveView.as_view(), name="account_inactive"),
    path("login/", login, name="account_login"),
    path("signup/", not_available, name="account_signup"),
    path("social/signup/", not_available, name="socialaccount_signup"),
    path("connections/", not_available, name="socialaccount_connections"),
]
