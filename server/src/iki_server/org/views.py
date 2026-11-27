"""Pages for teams and people. Projects are the web app's pages (``web/app``).

Every view asks the permission service before showing or changing
anything. Projects and teams someone can't see answer 404, so their
existence doesn't leak. Things they can see but not change answer 403.
"""

import contextlib
from collections.abc import Callable
from functools import wraps
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.views.decorators.http import require_http_methods, require_POST

from iki_server.accounts import service_accounts as service_account_services
from iki_server.accounts.models import IdentityProvider, Kind, User
from iki_server.oauth import services as oauth_services
from iki_server.oauth.forms import CredentialForm
from iki_server.org import holds, permissions, registries, services
from iki_server.org.forms import (
    AddTeamMemberForm,
    AppointListCuratorForm,
    AppointRegistrarForm,
    LegalHoldForm,
    LegalHoldReleaseForm,
    OrgRolesForm,
    RegistryForm,
    ServiceAccountForm,
    ServiceAccountRolesForm,
    TeamForm,
    list_choices,
)
from iki_server.org.models import (
    LegalHold,
    OrgRole,
    OrgRoleAssignment,
    ProjectMembership,
    ProjectRole,
    Registry,
    RegistryRole,
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
            acts_for_people=credential_form.cleaned_data["acts_for_people"],
        )
        credential_form = CredentialForm()
    if action in ("allow_acting", "stop_acting"):
        credential = get_object_or_404(
            oauth_services.credentials(account), pk=request.POST.get("credential")
        )
        oauth_services.set_acts_for_people(credential, action == "allow_acting", by=me)
        messages.success(
            request,
            f"People can now let {credential.display_name()} act for them."
            if action == "allow_acting"
            else f"{credential.display_name()} no longer acts for anyone. Their delegations "
            "to it ended.",
        )
        return redirect(request.path)
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


# --- Legal holds ---------------------------------------------------------------


@page("GET", "POST")
@refusals
def legal_holds(request: HttpRequest) -> HttpResponse:
    """Org Admins put projects on hold and release them; Auditors can read them (ADR 0092)."""
    me = _user(request)
    manage = permissions.can(me, Action.ORG_MANAGE)
    if not manage:
        permissions.require(me, Action.AUDIT_READ)
    form = LegalHoldForm(request.POST if request.POST.get("action") == "place" else None)
    # The hold being released, if one is: its row asks why, with the reason given so far.
    releasing: LegalHold | None = None
    release_form = LegalHoldReleaseForm(auto_id="id_release_%s")
    if request.method == "GET" and manage and (asked := request.GET.get("release")):
        with contextlib.suppress(ValidationError, ValueError):
            releasing = LegalHold.objects.filter(pk=asked, released_at=None).first()
    if request.method == "POST":
        if not manage:
            raise PermissionDenied
        if request.POST.get("action") == "release":
            hold = get_object_or_404(LegalHold, pk=request.POST.get("hold"))
            release_form = LegalHoldReleaseForm(request.POST, auto_id="id_release_%s")
            if release_form.is_valid():
                try:
                    holds.release(me, hold, reason=release_form.cleaned_data["reason"])
                except services.Refused as refusal:
                    release_form.add_error(
                        "reason" if refusal.field == "reason" else None, str(refusal)
                    )
                else:
                    messages.success(
                        request, f"Released the hold on {hold.project.name} for {hold.matter}."
                    )
                    return redirect("org:legal_holds")
            releasing = hold
        elif form.is_valid():
            try:
                hold = holds.place(
                    me,
                    form.cleaned_data["project"],
                    form.cleaned_data["matter"],
                    form.cleaned_data["note"],
                )
            except services.Refused as refusal:
                form.add_error(refusal.field or None, str(refusal))
            else:
                messages.success(
                    request,
                    f"Put {hold.project.name} on hold for {hold.matter}. Nothing in it will be "
                    "deleted until it’s released.",
                )
                return redirect("org:legal_holds")
    every = LegalHold.objects.select_related("project__team", "placed_by", "released_by").order_by(
        "-placed_at"
    )
    return render(
        request,
        "org/legal_holds.html",
        {
            "on": [h for h in every if h.released_at is None],
            "released": [h for h in every if h.released_at is not None][:50],
            "form": form,
            "manage": manage,
            "releasing": releasing,
            "release_form": release_form,
        },
    )


# --- Registries (ADR 0115) -------------------------------------------------------


@page("GET", "POST")
@refusals
def registry_list(request: HttpRequest) -> HttpResponse:
    """Org Admins add registries, each an identity boundary of its own, and say which teams'
    projects may be in each."""
    me = _user(request)
    permissions.require(me, Action.REGISTRIES_MANAGE)
    Registry.default()
    form = RegistryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            made = registries.create_registry(
                form.cleaned_data["name"],
                form.cleaned_data["description"],
                all_teams=form.cleaned_data["all_teams"],
                teams=form.cleaned_data["teams"],
                by=me,
            )
        except services.Refused as refusal:
            form.add_error(refusal.field or None, str(refusal))
        else:
            messages.success(request, f"Added {made.name}. Appoint its registrars next.")
            return redirect("org:registry", registry_id=made.pk)
    found = (
        Registry.objects.annotate(
            project_count=Count("projects", distinct=True),
            registrar_count=Count(
                "roles", filter=Q(roles__role=RegistryRole.Role.REGISTRAR), distinct=True
            ),
        )
        .prefetch_related(Prefetch("teams", queryset=Team.objects.order_by("name")))
        .order_by("-is_default", "name")
    )
    return render(request, "org/registry_list.html", {"registries": found, "form": form})


def _lab_lists(found: Registry, curators: list[RegistryRole]) -> list[dict[str, Any]]:
    """The installed lab lists (ADR 0270), by label, each with its curators in the registry."""
    from iki_server.entities import lists

    by_list: dict[str, list[RegistryRole]] = {}
    for grant in curators:
        by_list.setdefault(grant.list_type or "", []).append(grant)
    every = sorted(lists.types().items(), key=lambda t: (t[1].label_plural.casefold(), t[0]))
    return [
        {
            "type": name,
            "label": t.label_plural,
            "app_path": f"/types/{name}/?registry={found.pk}",
            "curators": by_list.get(name, []),
        }
        for name, t in every
    ]


@page("GET", "POST")
@refusals
def registry_detail(request: HttpRequest, registry_id: str) -> HttpResponse:
    me = _user(request)
    permissions.require(me, Action.REGISTRIES_MANAGE)
    found = get_object_or_404(Registry, pk=registry_id)
    action = request.POST.get("action")
    form = RegistryForm(
        request.POST if action == "save" else None,
        initial={
            "name": found.name,
            "description": found.description,
            "all_teams": found.all_teams,
            "teams": list(found.teams.all()),
        },
    )
    appoint = AppointRegistrarForm(request.POST if action == "appoint" else None)
    curate = AppointListCuratorForm(
        request.POST if action == "appoint_curator" else None, prefix="curator"
    )
    if request.method == "POST":
        if action == "save" and form.is_valid():
            try:
                registries.update_registry(
                    found,
                    by=me,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                    all_teams=form.cleaned_data["all_teams"],
                    teams=form.cleaned_data["teams"],
                )
            except services.Refused as refusal:
                form.add_error(refusal.field or None, str(refusal))
            else:
                messages.success(request, "Saved.")
                return redirect(request.path)
        elif action == "appoint" and appoint.is_valid():
            person = appoint.cleaned_data["person"]
            try:
                _, made = registries.grant_role(found, RegistryRole.Role.REGISTRAR, person, by=me)
            except services.Refused as refusal:
                appoint.add_error("person", str(refusal))
            else:
                if made:
                    messages.success(
                        request, f"{person.get_full_name()} is a registrar of {found.name}."
                    )
                else:
                    messages.info(
                        request, f"{person.get_full_name()} is one of its registrars already."
                    )
                return redirect(request.path)
        elif action == "appoint_curator" and curate.is_valid():
            person = curate.cleaned_data["person"]
            list_type = curate.cleaned_data["list_type"]
            label = dict(list_choices()).get(list_type, list_type)
            try:
                _, made = registries.grant_role(
                    found, RegistryRole.Role.LIST_CURATOR, person, by=me, list_type=list_type
                )
            except services.Refused as refusal:
                curate.add_error("list_type" if refusal.field == "list" else "person", str(refusal))
            else:
                if made:
                    messages.success(
                        request, f"{person.get_full_name()} curates {label} in {found.name}."
                    )
                else:
                    messages.info(request, f"{person.get_full_name()} curates {label} already.")
                return redirect(f"{request.path}#list-curators-title")
        elif action == "remove":
            from iki_server.entities import lists

            try:
                role_id = UUID(request.POST.get("role", ""))
            except ValueError:
                raise Http404("No such role in this registry.") from None
            grant = get_object_or_404(
                RegistryRole.objects.select_related("registry", "user"), registry=found, pk=role_id
            )
            person = grant.user
            if registries.remove_role(grant, by=me):
                if grant.list_type is None:
                    messages.success(
                        request,
                        f"{person.get_full_name()} is no longer a registrar of {found.name}.",
                    )
                else:
                    listed = lists.types().get(grant.list_type)
                    messages.success(
                        request,
                        f"{person.get_full_name()} no longer curates "
                        f"{listed.label_plural if listed else grant.list_type} in {found.name}.",
                    )
            if grant.list_type is not None:
                return redirect(f"{request.path}#list-curators-title")
            return redirect(request.path)
    grants = list(
        found.roles.select_related("user", "granted_by").order_by("user__name", "user__email")
    )
    registrars = [g for g in grants if g.role == RegistryRole.Role.REGISTRAR]
    projects = found.projects.select_related("team").order_by("team__name", "name")
    return render(
        request,
        "org/registry_detail.html",
        {
            "registry": found,
            "form": form,
            "appoint": appoint,
            "curate": curate,
            "lab_lists": _lab_lists(
                found, [g for g in grants if g.role == RegistryRole.Role.LIST_CURATOR]
            ),
            "registrars": registrars,
            "projects": projects,
            "is_registrar": any(r.user_id == me.pk for r in registrars),
        },
    )
