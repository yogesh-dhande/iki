"""The Org Admin's sign-in and provisioning page: identity provider, SCIM token, group links."""

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from iki_server.accounts.models import IdentityProvider, Kind, User
from iki_server.org import permissions
from iki_server.org import services as org_services
from iki_server.org.models import Team
from iki_server.org.permissions import Action
from iki_server.org.views import _user, page, refusals
from iki_server.scim import services, tokens
from iki_server.scim.models import ScimGroup

#: Warn this long before the SAML signing certificate expires.
CERTIFICATE_WARNING = timedelta(days=30)
NEW_TEAM = "new"


def _scim_base_url(request: HttpRequest) -> str:
    return request.build_absolute_uri(reverse("scim:users")).removesuffix("Users")


@page("GET", "POST")
@refusals
def sign_in_settings(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    new_token = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_token":
            # Shown on this response only; never stored or redirected with.
            new_token = tokens.create(by=me)
        elif action == "revoke_token":
            if tokens.revoke(by=me):
                messages.success(request, "Revoked the SCIM token. Provisioning is off.")
            return redirect(request.path)
    provider = IdentityProvider.objects.filter(is_enabled=True).first()
    expires = provider.saml_certificate_expires_at if provider else None
    groups = ScimGroup.objects.select_related("team").annotate(size=Count("memberships"))
    return render(
        request,
        "scim/settings.html",
        {
            "provider": provider,
            "certificate_expiring": expires is not None
            and expires - timezone.now() < CERTIFICATE_WARNING,
            "token": tokens.live(),
            "new_token": new_token,
            "scim_url": _scim_base_url(request),
            "groups": groups,
            "teams": Team.objects.filter(archived_at__isnull=True).order_by("name"),
            "provisioned": User.objects.filter(provisioned_at__isnull=False).count(),
            "not_provisioned": User.objects.filter(
                kind=Kind.PERSON, provisioned_at__isnull=True, is_active=True
            ).count(),
        },
    )


@page("GET", "POST")
@refusals
def group_link(request: HttpRequest, group_id: str) -> HttpResponse:
    """Review and confirm linking a group to a team, or unlink it."""
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    group = get_object_or_404(ScimGroup.objects.select_related("team"), pk=group_id)
    choice = request.POST.get("team") or request.GET.get("team") or ""
    if request.method == "POST" and request.POST.get("action") == "unlink":
        team = group.team
        services.unlink_group(group, by=me)
        if team is not None:
            messages.success(
                request,
                f"Unlinked {group.display_name}. {team.name} keeps its current members.",
            )
        return redirect("identity:sign_in_settings")
    team = None
    if choice and choice != NEW_TEAM:
        team = get_object_or_404(Team.objects.filter(archived_at__isnull=True), pk=choice)
    elif choice != NEW_TEAM:
        return redirect("identity:sign_in_settings")
    if request.method == "POST" and request.POST.get("action") == "link":
        if team is None:
            team = org_services.create_team(group.display_name, "", by=me)
        services.link_group(group, team, by=me)
        messages.success(
            request, f"Linked {group.display_name} to {team.name}. It now decides who is on it."
        )
        return redirect("org:team_detail", slug=team.slug)
    if team is None:
        joining, leaving = list(group.members.order_by("email")), []
    else:
        preview = services.preview_link(group, team)
        joining, leaving = preview.joining, preview.leaving
    return render(
        request,
        "scim/group_link.html",
        {
            "group": group,
            "team": team,
            "choice": choice,
            "joining": joining,
            "leaving": leaving,
            "other_groups": list(
                ScimGroup.objects.filter(team=team).exclude(pk=group.pk) if team else []
            ),
        },
    )
