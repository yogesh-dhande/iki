"""Pages for OAuth apps: the Org Admin's app list and approvals, and each person's
authorized apps."""

from uuid import UUID

from django.contrib import messages
from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
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


#: Events a page of a grant's activity shows.
GRANT_PAGE = 50


@page("GET")
def my_grant(request: HttpRequest, grant: UUID) -> HttpResponse:
    """What an app did for the person with one of their grants (ADR 0072), revoked or
    not: their own events only."""
    from iki_server.audit.log import ACTIONS
    from iki_server.audit.models import Agent, AuditEvent

    me = _user(request)
    events = AuditEvent.objects.filter(actor=me, agent__grant=grant).select_related(
        "project", "agent"
    )
    current = Consent.objects.filter(user=me, grant=grant).select_related("client").first()
    if current is None and not events.exists():
        raise Http404  # not theirs, or it never did anything
    if current is not None:
        app = current.client.display_name()
    else:
        first = Agent.objects.filter(grant=grant).order_by("first_seen_at").first()
        app = first.client_name if first is not None else ""
    before = request.GET.get("before")
    if before:
        anchor = AuditEvent.objects.filter(pk=before).values("occurred_at", "id").first()
        if anchor:
            events = events.filter(
                Q(occurred_at__lt=anchor["occurred_at"])
                | Q(occurred_at=anchor["occurred_at"], id__lt=anchor["id"])
            )
    shown = list(events.order_by("-occurred_at", "-id")[: GRANT_PAGE + 1])
    older = str(shown[GRANT_PAGE - 1].pk) if len(shown) > GRANT_PAGE else ""
    shown = shown[:GRANT_PAGE]
    return render(
        request,
        "oauth/my_grant.html",
        {
            "app": app,
            "grant": grant,
            "current": current is not None,
            "events": [(e, ACTIONS.get(e.action, e.action)) for e in shown],
            "older": older,
        },
    )
