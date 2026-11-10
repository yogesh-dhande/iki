"""Pages for OAuth apps: the Org Admin's app list and approvals, and each person's
authorized apps."""

from django.contrib import messages
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from iki_server.oauth import services
from iki_server.oauth.models import Approval, Client, Consent
from iki_server.oauth.scopes import SCOPES
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.views import _user, page, refusals


def _people_apps() -> QuerySet[Client]:
    """Apps people sign in to: everything except service-account credentials."""
    return Client.objects.filter(service_account__isnull=True).annotate(
        people=Count("consents", distinct=True)
    )


@page("GET", "POST")
@refusals
def org_apps(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    if request.method == "POST":
        if request.POST.get("action") == "policy":
            required = request.POST.get("require_approval") == "on"
            services.set_requires_approval(required, by=me)
            messages.success(
                request,
                "New apps now wait for an Org Admin’s approval."
                if required
                else "New apps can now be used without approval.",
            )
        return redirect(request.path)
    apps = _people_apps().order_by("-requested_at", "-created")
    return render(
        request,
        "oauth/org_apps.html",
        {
            "pending": apps.filter(approval=Approval.PENDING),
            "approved": apps.filter(approval=Approval.APPROVED),
            "blocked": apps.filter(approval=Approval.BLOCKED),
            "requires_approval": services.requires_approval(),
            "requested_count": apps.filter(
                approval=Approval.PENDING, requested_by__isnull=False
            ).count(),
        },
    )


@page("GET", "POST")
@refusals
def org_app(request: HttpRequest, client_id: int) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    client = get_object_or_404(_people_apps(), pk=client_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("approve", "block"):
            services.set_approval(
                client, Approval.APPROVED if action == "approve" else Approval.BLOCKED, by=me
            )
            verb = "Approved" if action == "approve" else "Blocked"
            messages.success(request, f"{verb} {client.display_name()}.")
        return redirect(request.path)
    consents = (
        Consent.objects.filter(client=client)
        .select_related("user")
        .prefetch_related("projects")
        .order_by("user__name", "user__email")
    )
    return render(
        request,
        "oauth/org_app.html",
        {
            "client": client,
            "consents": consents,
            "redirect_uris": client.redirect_uris.split(),
            "scope_descriptions": SCOPES,
        },
    )


@page("GET", "POST")
@refusals
def my_apps(request: HttpRequest) -> HttpResponse:
    me = _user(request)
    consents = (
        Consent.objects.filter(user=me)
        .select_related("client")
        .prefetch_related("projects")
        .order_by("client__name")
    )
    if request.method == "POST" and request.POST.get("action") == "revoke":
        consent = get_object_or_404(consents, pk=request.POST.get("consent"))
        services.revoke_consent(consent, by=me)
        messages.success(
            request, f"Revoked {consent.client.display_name()}’s access. It’s signed out."
        )
        return redirect(request.path)
    return render(
        request,
        "oauth/my_apps.html",
        {"consents": consents, "scope_descriptions": SCOPES},
    )
