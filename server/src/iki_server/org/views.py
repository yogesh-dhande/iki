"""Pages for projects, teams and people.

Every view asks the permission service before showing or changing
anything. Projects and teams someone can't see answer 404, so their
existence doesn't leak. Things they can see but not change answer 403.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.views.decorators.http import require_http_methods, require_POST

from iki_server.accounts.models import IdentityProvider, User
from iki_server.org import permissions, services
from iki_server.org.forms import (
    AddPersonForm,
    AddTeamForm,
    AddTeamMemberForm,
    ChangeRoleForm,
    OrgRolesForm,
    ProjectForm,
    ProjectSettingsForm,
    TeamForm,
)
from iki_server.org.models import (
    PROJECT_ROLE_DESCRIPTIONS,
    OrgRole,
    OrgRoleAssignment,
    Project,
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


def _project(request: HttpRequest, slug: str, action: Action) -> Project:
    project = get_object_or_404(Project.objects.select_related("team"), slug=slug)
    if not permissions.can(request.user, Action.PROJECT_SEE, project):
        raise Http404
    permissions.require(request.user, action, project)
    return project


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


# --- Home ----------------------------------------------------------------------


@login_required
def home(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    projects = permissions.visible_projects(user).filter(archived_at__isnull=True)
    mine = [p for p in projects if getattr(p, "my_role", None) is not None]
    return render(
        request,
        "org/home.html",
        {
            "projects": mine,
            "other_projects": len(projects) - len(mine),
            "can_create_project": permissions.teams_for_new_project(user).exists(),
            "role_labels": dict(ProjectRole.choices),
        },
    )


# --- Projects ------------------------------------------------------------------


@page("GET")
def project_list(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    projects = permissions.visible_projects(user).order_by("archived_at", "team__name", "name")
    return render(
        request,
        "org/project_list.html",
        {
            "projects": projects,
            "can_create_project": permissions.teams_for_new_project(user).exists(),
            "role_labels": dict(ProjectRole.choices),
        },
    )


@page("GET", "POST")
@refusals
def project_new(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    teams = permissions.teams_for_new_project(user)
    initial = {}
    if team_slug := request.GET.get("team"):
        initial["team"] = teams.filter(slug=team_slug).first()
    form = ProjectForm(request.POST or None, teams=teams, initial=initial)
    if request.method == "POST" and form.is_valid():
        team = form.cleaned_data["team"]
        permissions.require(user, Action.PROJECT_CREATE, team)
        try:
            project = services.create_project(
                team, form.cleaned_data["name"], form.cleaned_data["description"], by=user
            )
        except services.Refused as refusal:
            form.add_error("name", str(refusal))
        else:
            messages.success(request, f"Created {project.name}. You’re its Admin.")
            return redirect("org:project_detail", slug=project.slug)
    return render(request, "org/project_new.html", {"form": form, "has_teams": teams.exists()})


@page("GET")
def project_detail(request: HttpRequest, slug: str) -> HttpResponse:
    project = _project(request, slug, Action.PROJECT_SEE)
    role = permissions.project_role(request.user, project)
    return render(
        request,
        "org/project_detail.html",
        {
            "project": project,
            "role": role,
            "role_description": PROJECT_ROLE_DESCRIPTIONS.get(role) if role else None,
            "can_read": permissions.can(request.user, Action.PROJECT_READ, project),
            "can_manage_project": permissions.can(request.user, Action.PROJECT_MANAGE, project),
            "can_manage_members": permissions.can(
                request.user, Action.PROJECT_MEMBERS_MANAGE, project
            ),
            "admin_count": project.memberships.filter(role=ProjectRole.ADMIN).count(),
        },
    )


@page("GET", "POST")
@refusals
def project_members(request: HttpRequest, slug: str) -> HttpResponse:
    project = _project(request, slug, Action.PROJECT_SEE)
    user = _user(request)
    can_manage = permissions.can(user, Action.PROJECT_MEMBERS_MANAGE, project)
    granted_teams = project.memberships.filter(team__isnull=False).values("team")
    teams = Team.objects.filter(archived_at__isnull=True).exclude(pk__in=granted_teams)
    person_form = AddPersonForm(prefix="person")
    team_form = AddTeamForm(prefix="team", teams=teams)

    if request.method == "POST":
        permissions.require(user, Action.PROJECT_MEMBERS_MANAGE, project)
        if request.POST.get("add") == "team":
            team_form = AddTeamForm(request.POST, prefix="team", teams=teams)
            if team_form.is_valid():
                team, role = team_form.cleaned_data["team"], team_form.cleaned_data["role"]
                services.grant_project_role(project, ProjectRole(role), team=team, by=user)
                messages.success(request, f"Everyone in {team.name} is now {role_label(role)}.")
                return redirect(request.path)
        else:
            person_form = AddPersonForm(request.POST, prefix="person")
            if person_form.is_valid():
                person, role = person_form.cleaned_data["person"], person_form.cleaned_data["role"]
                services.grant_project_role(project, ProjectRole(role), user=person, by=user)
                messages.success(request, f"{person.get_full_name()} is now {role_label(role)}.")
                return redirect(request.path)

    memberships = project.memberships.select_related("user", "team").annotate(
        team_size=Count("team__memberships")
    )
    return render(
        request,
        "org/project_members.html",
        {
            "project": project,
            "memberships": memberships,
            "can_manage": can_manage,
            "can_manage_project": permissions.can(user, Action.PROJECT_MANAGE, project),
            "person_form": person_form,
            "team_form": team_form,
            "has_teams_to_add": teams.exists(),
            "roles": ProjectRole.choices,
            "role_descriptions": [(r.label, PROJECT_ROLE_DESCRIPTIONS[r]) for r in ProjectRole],
            "can_join": can_manage and permissions.project_role(user, project) != ProjectRole.ADMIN,
        },
    )


def role_label(role: int) -> str:
    label = ProjectRole(role).label
    return f"an {label}" if label[0] in "AEIOU" else f"a {label}"


@login_required
@require_POST
def project_member(request: HttpRequest, slug: str, membership_id: str) -> HttpResponse:
    project = _project(request, slug, Action.PROJECT_MEMBERS_MANAGE)
    membership = get_object_or_404(
        ProjectMembership.objects.select_related("user", "team"), pk=membership_id, project=project
    )
    who = membership.user.get_full_name() if membership.user else str(membership.team)
    try:
        if request.POST.get("action") == "remove":
            services.remove_project_membership(membership, by=_user(request))
            messages.success(request, f"Removed {who} from {project.name}.")
        else:
            form = ChangeRoleForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Choose a role.")
            else:
                role = form.cleaned_data["role"]
                services.grant_project_role(
                    project,
                    ProjectRole(role),
                    user=membership.user,
                    team=membership.team,
                    by=_user(request),
                )
                messages.success(request, f"{who} is now {role_label(role)}.")
    except services.Refused as refusal:
        messages.error(request, str(refusal))
    return redirect("org:project_members", slug=project.slug)


@login_required
@require_POST
def project_join(request: HttpRequest, slug: str) -> HttpResponse:
    """An Org Admin adds themselves as Admin, for example to recover an orphaned project."""
    project = _project(request, slug, Action.PROJECT_MEMBERS_MANAGE)
    user = _user(request)
    services.grant_project_role(project, ProjectRole.ADMIN, user=user, by=user)
    messages.success(request, f"You’re now an Admin of {project.name}.")
    return redirect("org:project_detail", slug=project.slug)


@page("GET", "POST")
@refusals
def project_settings(request: HttpRequest, slug: str) -> HttpResponse:
    project = _project(request, slug, Action.PROJECT_MANAGE)
    user = _user(request)
    action = request.POST.get("action")
    if request.method == "POST" and action in ("archive", "restore"):
        services.set_project_archived(project, action == "archive", by=user)
        verb = "Archived" if action == "archive" else "Restored"
        messages.success(request, f"{verb} {project.name}.")
        return redirect("org:project_detail", slug=project.slug)
    form = ProjectSettingsForm(
        request.POST or None, initial={"name": project.name, "description": project.description}
    )
    if request.method == "POST":
        if project.is_archived:
            messages.error(request, "Restore the project before changing it.")
            return redirect(request.path)
        if form.is_valid():
            try:
                services.update_project(
                    project, form.cleaned_data["name"], form.cleaned_data["description"], by=user
                )
            except services.Refused as refusal:
                form.add_error("name", str(refusal))
            else:
                messages.success(request, "Saved.")
                return redirect("org:project_detail", slug=project.slug)
    return render(
        request,
        "org/project_settings.html",
        {"project": project, "form": form, "can_manage_project": True},
    )


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
        User.objects.order_by("-is_active", "name", "email")
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
