"""The OAuth 2.1 endpoints and the pages people see during sign-in (ADR 0028).

- ``/oauth/authorize/``: the consent page (authorization code with PKCE).
- ``/oauth/token/``, ``/oauth/revoke/``: for clients.
- ``/oauth/device-authorization/`` and ``/oauth/device/``: the device flow
  the CLI and SDK use (RFC 8628). People type the code shown in their
  terminal, then choose what to allow.
- ``/oauth/register/``: Dynamic Client Registration (RFC 7591), for the
  authorization code flow only.
- ``/.well-known/oauth-authorization-server`` (RFC 8414) and
  ``/.well-known/oauth-protected-resource`` (RFC 9728).

A client someone hasn't been allowed to use yet (waiting for an Org Admin,
or blocked) gets a page that says so, instead of an OAuth error.
"""

import json
from typing import Any
from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from oauth2_provider import views as dot
from oauth2_provider.models import (
    DeviceCodeResponse,
    DeviceRequest,
    create_device_grant,
    get_device_grant_model,
)
from oauth2_provider.settings import oauth2_settings

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.oauth import redirects, services
from iki_server.oauth.forms import ConsentForm, DeviceCodeForm, DeviceConsentForm
from iki_server.oauth.models import Approval, Client
from iki_server.oauth.scopes import DEFAULT_SCOPES, SCOPES, ordered
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.web.ratelimit import RateLimiter, client_key

DeviceGrant = get_device_grant_model()

#: Self-registrations one client address may make per minute.
register_limiter = RateLimiter("oauth_register", limit_per_minute=10)
#: Token requests one client address may make per minute.
token_limiter = RateLimiter("oauth_token", limit_per_minute=120)
#: Device codes one client address may ask for per minute.
device_authorization_limiter = RateLimiter("oauth_device_authorization", limit_per_minute=30)
#: Device codes one person may try per minute.
device_code_limiter = RateLimiter("oauth_device_code", limit_per_minute=10)

SELF_REGISTERED_GRANT_TYPES = {"authorization_code", "refresh_token"}


def _user(request: HttpRequest) -> User:
    user = request.user
    assert isinstance(user, User)  # noqa: S101 - every page here needs sign-in
    return user


def my_projects(user: User) -> QuerySet[Project]:
    """Projects someone has a role in and could let a client use."""
    return (
        permissions.visible_projects(user)
        .filter(my_role__isnull=False, archived_at__isnull=True)  # type: ignore[misc]
        .order_by("team__name", "name")
    )


def _origin(uri: str) -> str | None:
    """The CSP source that allows a redirect to ``uri``."""
    parts = urlsplit(uri)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    if parts.scheme:
        return f"{parts.scheme}:"  # a native app's private-use scheme (RFC 8252 §7.1)
    return None


def _client_facts(client: Client) -> dict[str, Any]:
    hosts = sorted({urlsplit(uri).netloc or uri for uri in client.redirect_uris.split()})
    return {
        "client": client,
        "redirect_hosts": hosts,
        "unverified": client.is_self_registered,
        "cimd_host": urlsplit(client.client_id).netloc
        if client.registration_source == Client.RegistrationSource.CIMD
        else "",
    }


def not_allowed_yet(request: HttpRequest, client: Client) -> HttpResponse:
    """The page for a client that is waiting for approval or blocked."""
    return render(
        request,
        "oauth/not_allowed.html",
        {**_client_facts(client), "requested": client.requested_by_id is not None},
        status=403,
    )


@login_required
@require_POST
def request_approval(request: HttpRequest, client_id: int) -> HttpResponse:
    client = get_object_or_404(Client, pk=client_id, approval=Approval.PENDING)
    services.request_approval(client, by=_user(request))
    return not_allowed_yet(request, client)


def _resolve(request: HttpRequest, client_id: str) -> Client | None:
    if oauth2_settings.CIMD_ENABLED:
        from oauth2_provider.cimd import is_cimd_client_id, resolve_cimd_application

        if is_cimd_client_id(client_id):
            try:
                resolve_cimd_application(client_id, request)
            except Exception:
                return None
    return Client.objects.filter(client_id=client_id).first()


@method_decorator(never_cache, name="dispatch")
class AuthorizeView(dot.AuthorizationView):
    template_name = "oauth/authorize.html"
    form_class = ConsentForm

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        client = _resolve(request, request.GET.get("client_id", ""))
        if client is not None and not client.is_approved:
            return not_allowed_yet(request, client)
        return super().get(request, *args, **kwargs)

    def _requested(self) -> list[str]:
        scope = self.request.POST.get("scope") or " ".join(self.oauth2_data.get("scopes", []) or [])
        return ordered(scope.split()) or list(DEFAULT_SCOPES)

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["requested"] = self._requested()
        kwargs["projects"] = my_projects(_user(self.request))
        return kwargs

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        initial["granted"] = self._requested()
        client_id = initial.get("client_id")
        client = Client.objects.filter(client_id=client_id).first() if client_id else None
        consent = services.consent_for(_user(self.request), client) if client else None
        if consent is not None:
            initial["access"] = "all" if consent.all_projects else "chosen"
            initial["projects"] = list(consent.projects.all())
        return initial

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        context = super().get_context_data(**kwargs)
        application = context.get("application")
        if isinstance(application, Client):
            context.update(_client_facts(application))
            context["has_projects"] = my_projects(_user(self.request)).exists()
            origin = _origin(str(context.get("redirect_uri") or ""))
            if origin:
                # Browsers apply form-action to the redirect after the form posts.
                self.request.iki_form_action_extra = [origin]
        return context

    def form_valid(self, form: ConsentForm) -> HttpResponse:
        if form.cleaned_data.get("allow"):
            client = Client.objects.get(client_id=form.cleaned_data["client_id"])
            if not client.is_approved:
                return not_allowed_yet(self.request, client)
            requested = set(self._requested())
            scopes = [s for s in form.cleaned_data["granted"] if s in requested]
            services.grant(
                _user(self.request),
                client,
                services.Grant(
                    scopes=scopes,
                    all_projects=form.cleaned_data["access"] == "all",
                    projects=list(form.cleaned_data["projects"]),
                ),
            )
            form.cleaned_data["scope"] = " ".join(scopes)
        response: HttpResponse = super().form_valid(form)
        return response


# --- Device flow ---------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def device(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    form = DeviceCodeForm(
        request.POST or None, initial={"user_code": request.GET.get("user_code", "")}
    )
    if request.method == "POST" and form.is_valid():
        if not device_code_limiter.allow(str(user.pk)):
            form.add_error("user_code", "Too many tries. Wait a minute, then try again.")
        else:
            grant = DeviceGrant.objects.filter(
                user_code=form.cleaned_data["user_code"], status=DeviceGrant.AUTHORIZATION_PENDING
            ).first()
            if grant is None or grant.is_expired() or grant.user_id not in (None, user.pk):
                form.add_error("user_code", "That code isn’t valid, or it has expired.")
            else:
                grant.user = user
                grant.save(update_fields=["user"])
                return redirect("oauth:device_confirm", grant_id=grant.pk)
    return render(request, "oauth/device.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def device_confirm(request: HttpRequest, grant_id: int) -> HttpResponse:
    user = _user(request)
    grant = get_object_or_404(
        DeviceGrant, pk=grant_id, user=user, status=DeviceGrant.AUTHORIZATION_PENDING
    )
    if grant.is_expired():
        return render(request, "oauth/device_done.html", {"expired": True})
    client = get_object_or_404(Client, client_id=grant.client_id)
    if not client.is_approved:
        return not_allowed_yet(request, client)
    requested = ordered(grant.scope.split()) or list(DEFAULT_SCOPES)
    consent = services.consent_for(user, client)
    form = DeviceConsentForm(
        request.POST or None,
        initial={
            "granted": requested,
            "access": "all" if consent and consent.all_projects else "chosen",
            "projects": list(consent.projects.all()) if consent else [],
        },
    )
    form.setup_choices(requested, my_projects(user))
    if request.method == "POST":
        if request.POST.get("action") == "deny":
            grant.status = DeviceGrant.DENIED
            grant.save(update_fields=["status"])
            return render(request, "oauth/device_done.html", {"denied": True, "client": client})
        if form.is_valid():
            scopes = [s for s in form.cleaned_data["granted"] if s in requested]
            services.grant(
                user,
                client,
                services.Grant(
                    scopes=scopes,
                    all_projects=form.cleaned_data["access"] == "all",
                    projects=list(form.cleaned_data["projects"]),
                ),
            )
            grant.scope = " ".join(scopes)
            grant.status = DeviceGrant.AUTHORIZED
            grant.save(update_fields=["scope", "status"])
            audit.record(
                "oauth.device_approved", actor=user, target=client, data={"scopes": scopes}
            )
            return render(request, "oauth/device_done.html", {"client": client})
    return render(
        request,
        "oauth/device_confirm.html",
        {
            **_client_facts(client),
            "form": form,
            "grant": grant,
            "has_projects": my_projects(user).exists(),
            "scope_descriptions": SCOPES,
        },
    )


# --- Endpoints for clients ----------------------------------------------------


def _oauth_error(error: str, description: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": error, "error_description": description}, status=status)


def _limited(limiter: RateLimiter, request: HttpRequest) -> JsonResponse | None:
    decision = limiter.check(client_key(request))
    if decision.allowed:
        return None
    response = _oauth_error("too_many_requests", "Too many requests. Wait, then try again.", 429)
    response["Retry-After"] = str(decision.retry_after)
    return response


def _registration_problem(body: bytes) -> JsonResponse | None:
    """Iki's limits on what a client may register for itself."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None  # DOT answers invalid JSON itself
    if not isinstance(data, dict):
        return None
    grant_types = data.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not set(grant_types) <= SELF_REGISTERED_GRANT_TYPES:
        return _oauth_error(
            "invalid_client_metadata",
            "Clients can register themselves for the authorization_code and refresh_token "
            "grants only. Service accounts get client credentials from an Org Admin.",
        )
    response_types = data.get("response_types", ["code"])
    if not isinstance(response_types, list) or not set(response_types) <= {"code"}:
        return _oauth_error("invalid_client_metadata", "Only the code response type is supported.")
    uris = data.get("redirect_uris", [])
    if isinstance(uris, list) and (bad := redirects.refused(str(uri) for uri in uris)):
        return _oauth_error(
            "invalid_redirect_uri",
            f"Redirect URIs must use https, or http to a loopback address "
            f"(127.0.0.1, [::1] or localhost) for apps on your own computer: {bad[0]}",
        )
    name = data.get("client_name", "")
    if not isinstance(name, str) or len(name) > 100:
        return _oauth_error("invalid_client_metadata", "client_name must be under 100 characters.")
    return None


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(dot.DynamicClientRegistrationView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        refused = _limited(register_limiter, request) or _registration_problem(request.body)
        if refused is not None:
            return refused
        response: HttpResponse = super().post(request, *args, **kwargs)
        return response


@method_decorator(csrf_exempt, name="dispatch")
class RegistrationManagementView(dot.DynamicClientRegistrationManagementView):
    def put(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        refused = _registration_problem(request.body)
        if refused is not None:
            return refused
        response: HttpResponse = super().put(request, *args, **kwargs)
        return response


@method_decorator(csrf_exempt, name="dispatch")
class DeviceAuthorizationView(dot.DeviceAuthorizationView):
    """RFC 8628 §3.1-3.2. Saves the scopes oauthlib resolved (the requested ones,
    already checked, or the client's defaults) with the device code."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        refused = _limited(device_authorization_limiter, request)
        if refused is not None:
            return refused
        headers, body, status = self.create_device_authorization_response(request)
        data = json.loads(body) if isinstance(body, str) else dict(body)
        if status != 200:
            return JsonResponse(data, status=status, headers=headers)
        scope = data.pop("scope", "") or " ".join(DEFAULT_SCOPES)
        create_device_grant(
            DeviceRequest(client_id=request.POST["client_id"], scope=scope),
            DeviceCodeResponse(**data),
        )
        return JsonResponse(data, status=status, headers=headers)


@method_decorator(csrf_exempt, name="dispatch")
class TokenView(dot.TokenView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        refused = _limited(token_limiter, request)
        if refused is not None:
            return refused
        # Every grant, the device code included, goes through oauthlib and
        # IkiValidator; DOT's own device-code shortcut checks less.
        response: HttpResponse = self.authorization_flow_token_response(request)
        return response


class ServerMetadataView(dot.OAuthServerMetadataView):
    """RFC 8414 metadata, plus the device authorization endpoint (RFC 8628 §4)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        response: HttpResponse = super().get(request, *args, **kwargs)
        data = json.loads(response.content)
        data["device_authorization_endpoint"] = request.build_absolute_uri(
            "/oauth/device-authorization/"
        )
        updated = JsonResponse(data)
        updated["Access-Control-Allow-Origin"] = "*"
        return updated
