"""Pages for teams and people. Projects are the web app's pages (``web/app``).

Every view asks the permission service before showing or changing
anything. Projects and teams someone can't see answer 404, so their
existence doesn't leak. Things they can see but not change answer 403.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.views.decorators.http import require_http_methods, require_POST

from iki_server.accounts import service_accounts as service_account_services
from iki_server.accounts.models import IdentityProvider, Kind, User
from iki_server.oauth import services as oauth_services
from iki_server.oauth.forms import CredentialForm
from iki_server.org import permissions, services
from iki_server.org.forms import (
    AddTeamMemberForm,
    OrgRolesForm,
    ServiceAccountForm,
    ServiceAccountRolesForm,
    TeamForm,
)
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    ProjectMembership,
    ProjectRole,
    Team,
    TeamMembership,
)
from iki_server.org.permissions import Action

View = Callable[..., HttpResponse]


def page(*methods: str) -> Callable[[View], View]:
    """A signed-in page answering only the given HTTP methods."""

    def decorate(view: View) -> View:
        return login_required(require_http_methods(list(methods))(view))

    return decorate


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # every view here requires sign-in
        raise PermissionDenied
    return user


def _team(request: HttpRequest, slug: str, action: Action) -> Team:
    team = get_object_or_404(Team, slug=slug)
    if not permissions.can(request.user, Action.TEAM_VIEW, team):
        raise Http404
    permissions.require(request.user, action, team)
    return team


def refusals(view: View) -> View:
    """Shows a refused change (a broken invariant) as an error message."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        try:
            return view(request, *args, **kwargs)
        except services.Refused as refusal:
            messages.error(request, str(refusal))
            return redirect(request.path)

    return wrapper


# --- Teams ---------------------------------------------------------------------


@page("GET", "POST")
def team_list(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    can_create = permissions.can(user, Action.TEAM_CREATE)
    form = TeamForm(request.POST or None)
    if request.method == "POST":
        permissions.require(user, Action.TEAM_CREATE)
        if form.is_valid():
            try:
                team = services.create_team(
                    form.cleaned_data["name"], form.cleaned_data["description"], by=user
                )
            except services.Refused as refusal:
                form.add_error("name", str(refusal))
            else:
                messages.success(request, f"Created {team.name}. Add its members next.")
                return redirect("org:team_detail", slug=team.slug)
    teams = (
        permissions.visible_teams(user)
        .annotate(member_count=Count("memberships", distinct=True))
        .order_by("archived_at", "name")
    )
    return render(
        request, "org/team_list.html", {"teams": teams, "form": form, "can_create": can_create}
    )


@page("GET", "POST")
@refusals
def team_detail(request: HttpRequest, slug: str) -> HttpResponse:
    team = _team(request, slug, Action.TEAM_VIEW)
    user = _user(request)
    can_manage = permissions.can(user, Action.TEAM_MANAGE, team)
    form = AddTeamMemberForm()
    if request.method == "POST":
        permissions.require(user, Action.TEAM_MANAGE, team)
        form = AddTeamMemberForm(request.POST)
        if form.is_valid():
            person = form.cleaned_data["person"]
            services.add_team_member(team, person, by=user)
            messages.success(request, f"Added {person.get_full_name()} to {team.name}.")
            return redirect(request.path)
    members = TeamMembership.objects.filter(team=team).select_related("user").order_by("user__name")
    projects = permissions.visible_projects(user).filter(team=team).order_by("archived_at", "name")
    return render(
        request,
        "org/team_detail.html",
        {
            "team": team,
            "members": members,
            "projects": projects,
            "form": form,
            "can_manage": can_manage,
            "linked_groups": services.linked_groups(team),
            "can_create_project": permissions.can(user, Action.PROJECT_CREATE, team),
            "role_labels": dict(ProjectRole.choices),
        },
    )


@login_required
@require_POST
def team_member_remove(request: HttpRequest, slug: str, user_id: str) -> HttpResponse:
    team = _team(request, slug, Action.TEAM_MANAGE)
    person = get_object_or_404(User, pk=user_id)
    try:
        services.remove_team_member(team, person, by=_user(request))
    except services.Refused as refusal:
        messages.error(request, str(refusal))
    else:
        messages.success(request, f"Removed {person.get_full_name()} from {team.name}.")
    return redirect("org:team_detail", slug=team.slug)


@page("GET", "POST")
@refusals
def team_settings(request: HttpRequest, slug: str) -> HttpResponse:
    team = _team(request, slug, Action.TEAM_MANAGE)
    user = _user(request)
    action = request.POST.get("action")
    if request.method == "POST" and action in ("archive", "restore"):
        services.set_team_archived(team, action == "archive", by=user)
        verb = "Archived" if action == "archive" else "Restored"
        messages.success(request, f"{verb} {team.name}.")
        return redirect("org:team_detail", slug=team.slug)
    form = TeamForm(
        request.POST or None, initial={"name": team.name, "description": team.description}
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_team(
                team, form.cleaned_data["name"], form.cleaned_data["description"], by=user
            )
        except services.Refused as refusal:
            form.add_error("name", str(refusal))
        else:
            messages.success(request, "Saved.")
            return redirect("org:team_detail", slug=team.slug)
    return render(request, "org/team_settings.html", {"team": team, "form": form})


# --- People --------------------------------------------------------------------


@page("GET")
def people(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    users = (
        User.objects.filter(kind=Kind.PERSON)
        .order_by("-is_active", "name", "email")
        .prefetch_related(
            Prefetch("org_roles", queryset=OrgRoleAssignment.objects.order_by("role"))
        )
        .annotate(team_count=Count("team_memberships", distinct=True))
    )
    return render(request, "org/people.html", {"people": users})


@page("GET", "POST")
@refusals
def person(request: HttpRequest, user_id: str) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    subject = get_object_or_404(User, pk=user_id)
    if subject.is_service_account:
        return redirect("org:service_account", user_id=subject.pk)
    current = set(OrgRoleAssignment.objects.filter(user=subject).values_list("role", flat=True))
    form = OrgRolesForm(request.POST or None, initial={"roles": sorted(current)})
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("deactivate", "activate"):
            services.set_active(subject, action == "activate", by=me)
            verb = "Reactivated" if action == "activate" else "Deactivated"
            messages.success(request, f"{verb} {subject.get_full_name()}.")
            return redirect(request.path)
        if action == "end_sessions":
            ended = services.end_sessions(subject, by=me)
            messages.success(
                request,
                f"Signed {subject.get_full_name()} out of {ended} session{pluralize(ended)}.",
            )
            return redirect(request.path)
        if form.is_valid():
            services.set_org_roles(subject, set(form.cleaned_data["roles"]), by=me)
            messages.success(request, f"Saved {subject.get_full_name()}’s org roles.")
            return redirect(request.path)
    teams = Team.objects.filter(memberships__user=subject).order_by("name")
    return render(
        request,
        "org/person.html",
        {
            "subject": subject,
            "form": form,
            "teams": teams,
            "is_self": subject.pk == me.pk,
            "org_admin": OrgRole.ORG_ADMIN in current,
            "provider": IdentityProvider.objects.filter(is_enabled=True).first(),
        },
    )


# --- Service accounts ----------------------------------------------------------


@page("GET", "POST")
@refusals
def service_accounts(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    form = ServiceAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            account = service_account_services.create(
                form.cleaned_data["name"], form.cleaned_data["description"], by=me
            )
        except services.Refused as refusal:
            form.add_error("name", str(refusal))
        else:
            messages.success(request, f"Created {account.name}. Give it roles in its projects.")
            return redirect("org:service_account", user_id=account.pk)
    accounts = (
        User.objects.filter(kind=Kind.SERVICE)
        .select_related("service_account")
        .prefetch_related(
            Prefetch("org_roles", queryset=OrgRoleAssignment.objects.order_by("role"))
        )
        .annotate(project_count=Count("project_memberships", distinct=True))
        .order_by("-is_active", "name")
    )
    return render(request, "org/service_accounts.html", {"accounts": accounts, "form": form})


@page("GET", "POST")
@refusals
def service_account(request: HttpRequest, user_id: str) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    account = get_object_or_404(
        User.objects.select_related("service_account__created_by"),
        pk=user_id,
        kind=Kind.SERVICE,
    )
    details = account.service_account
    current = set(OrgRoleAssignment.objects.filter(user=account).values_list("role", flat=True))
    action = request.POST.get("action") if request.method == "POST" else None
    form = ServiceAccountForm(
        request.POST if action == "save" else None,
        initial={"name": account.name, "description": details.description},
    )
    roles_form = ServiceAccountRolesForm(
        request.POST if action == "roles" else None, initial={"roles": sorted(current)}
    )
    credential_form = CredentialForm(request.POST if action == "create_credential" else None)
    new_credential = None
    if action in ("deactivate", "activate"):
        services.set_active(account, action == "activate", by=me)
        verb = "Reactivated" if action == "activate" else "Deactivated"
        messages.success(request, f"{verb} {account.name}.")
        return redirect(request.path)
    if action == "create_credential" and credential_form.is_valid():
        # The secret is shown on this response only; never stored in plain text.
        new_credential = oauth_services.create_credential(
            account,
            credential_form.cleaned_data["name"],
            list(credential_form.cleaned_data["scopes"]),
            by=me,
        )
        credential_form = CredentialForm()
    if action == "revoke_credential":
        credential = get_object_or_404(
            oauth_services.credentials(account), pk=request.POST.get("credential")
        )
        oauth_services.revoke_credential(credential, by=me)
        messages.success(request, f"Revoked {credential.display_name()}.")
        return redirect(request.path)
    if action == "save" and form.is_valid():
        try:
            service_account_services.update(
                account,
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
                by=me,
            )
        except services.Refused as refusal:
            form.add_error("name", str(refusal))
        else:
            messages.success(request, "Saved.")
            return redirect(request.path)
    if action == "roles" and roles_form.is_valid():
        services.set_org_roles(account, set(roles_form.cleaned_data["roles"]), by=me)
        messages.success(request, f"Saved {account.name}’s org roles.")
        return redirect(request.path)
    grants = (
        ProjectMembership.objects.filter(user=account)
        .select_related("project__team")
        .order_by("project__name")
    )
    return render(
        request,
        "org/service_account.html",
        {
            "account": account,
            "details": details,
            "form": form,
            "roles_form": roles_form,
            "grants": grants,
            "credential_form": credential_form,
            "new_credential": new_credential,
            "credentials": oauth_services.credentials(account),
            "token_url": request.build_absolute_uri("/oauth/token/"),
            "token_minutes": settings.OAUTH2_PROVIDER["ACCESS_TOKEN_EXPIRE_SECONDS"] // 60,  # type: ignore[operator]
        },
    )
