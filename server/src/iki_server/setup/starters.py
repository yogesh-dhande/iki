"""Recipes installed with active plugins and applied to projects (ADR 0198)."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from itertools import combinations
from typing import Any, Protocol
from uuid import UUID

from django.db import transaction
from django.http import Http404

from iki_server.accounts.models import Kind, User
from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.org import lab_roles, permissions, services
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.setup import dose_response
from iki_server.setup.models import StarterApplication


class Examples(Protocol):
    def check_example(self, project: Project, requester_project: Project, *, by: User) -> None: ...
    def seed_example(
        self, project: Project, requester_project: Project, *, by: User
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class Starter:
    id: str
    version: str
    label: str
    description: str
    plugin: str
    setting: str
    result_base: str
    flows: dict[str, str]
    requested_types: dict[str, str]
    types: tuple[str, ...]
    roles: dict[str, ProjectRole]
    required_roles: dict[str, tuple[str, ...]]
    independent_roles: dict[str, tuple[str, ...]]
    example_keys: tuple[str, ...]
    examples: Examples


PLATE = "iki/plate_assays."
COORDINATOR = "iki/example_lims.coordinator"
ANALYST, REVIEWER, QA = PLATE + "analyst", PLATE + "reviewer", PLATE + "qa"
FLOW_LABELS = {"research": "Research", "regulated_qc": "Regulated QC"}

DOSE_RESPONSE = Starter(
    id="dose-response",
    version="1.0.0",
    label="Dose-response plate assay",
    description="Receive an example request, map a reader file to wells, fit a curve and return "
    "the signed result to the requester.",
    plugin="iki/plate_assays",
    setting="review_flow",
    result_base=PLATE + "AssayResult",
    flows={"research": PLATE + "ResearchAssayResult", "regulated_qc": PLATE + "QCAssayResult"},
    requested_types={
        "research": PLATE + "RequestedResearchAssayResult",
        "regulated_qc": PLATE + "RequestedQCAssayResult",
    },
    types=(
        "iki/protein.Protein",
        "iki/example_lims.Sample",
        "iki/example_lims.Tube",
        "iki/example_lims.AnalysisRequest",
        PLATE + "AssayPlate",
        PLATE + "PlateRead",
        PLATE + "WellMeasurement",
        PLATE + "AssayResult",
        PLATE + "ResearchAssayResult",
        PLATE + "QCAssayResult",
        PLATE + "RequestedResearchAssayResult",
        PLATE + "RequestedQCAssayResult",
    ),
    roles={
        COORDINATOR: ProjectRole.EDITOR,
        ANALYST: ProjectRole.EDITOR,
        REVIEWER: ProjectRole.VIEWER,
        QA: ProjectRole.VIEWER,
    },
    required_roles={
        "research": (COORDINATOR, ANALYST, REVIEWER),
        "regulated_qc": (COORDINATOR, ANALYST, REVIEWER, QA),
    },
    independent_roles={"regulated_qc": (ANALYST, REVIEWER, QA)},
    example_keys=("request", "protein", "sample", "tube", "reader_file", "plate"),
    examples=dose_response,
)
CATALOG = (DOSE_RESPONSE,)


@cache
def installed_starters() -> dict[str, Starter]:
    """Recognized recipe versions declared by this build, with their required types active."""
    plugins = registry.plugins()
    types = registry.types()
    detached = registry.detached()
    roles = lab_roles.declared()
    return {
        s.id: s
        for s in CATALOG
        if s.plugin in plugins
        and plugins[s.plugin].starters.get(s.id) == s.version
        and s.setting in plugins[s.plugin].settings
        and s.roles.keys() <= roles.keys()
        and all(t in types and types[t].plugin not in detached for t in s.types)
    }


def effective_flow(project: Project, starter_id: str) -> str | None:
    """The project's applied choice, also when an installed recipe version has changed."""
    return (
        StarterApplication.objects.filter(project=project, starter=starter_id)
        .values_list("flow", flat=True)
        .first()
    )


def list_starters(
    project: Project | None = None, *, by: User | None = None
) -> list[dict[str, Any]]:
    current = (
        {
            a.starter: a
            for a in StarterApplication.objects.filter(project=project).select_related(
                "requester_project"
            )
        }
        if project
        else {}
    )
    may_manage = bool(by and project and permissions.can(by, Action.PROJECT_MANAGE, project))
    labels = lab_roles.labels()
    return [
        {
            "id": s.id,
            "version": s.version,
            "label": s.label,
            "description": s.description,
            "flows": [
                {
                    "id": f,
                    "label": FLOW_LABELS[f],
                    "result_type": t,
                    "required_roles": list(s.required_roles[f]),
                }
                for f, t in s.requested_types.items()
            ],
            "types": list(s.types),
            "roles": [
                {"id": r, "label": labels[r], "minimum_project_role": minimum.name.lower()}
                for r, minimum in s.roles.items()
            ],
            "applied": {
                "version": a.version,
                "flow": a.flow,
                "requester_project": str(a.requester_project_id)
                if may_manage and permissions.can(by, Action.PROJECT_EDIT, a.requester_project)
                else None,
            }
            if (a := current.get(s.id))
            else None,
        }
        for s in installed_starters().values()
    ]


RoleAssignments = Mapping[str, Sequence[UUID]]


def _members(
    project: Project, starter: Starter, flow: str, assignments: dict[str, set[UUID]]
) -> dict[UUID, ProjectMembership]:
    for role in assignments:
        if role not in starter.roles:
            raise Refused(f"{role} is not a role in this starter.", field="role_assignments")
    for role in starter.required_roles[flow]:
        if not assignments.get(role):
            raise Refused(
                f"Choose a project member for {lab_roles.label(role)}.", field="role_assignments"
            )
    member_ids = {pk for group in assignments.values() for pk in group}
    memberships = (
        ProjectMembership.objects.filter(
            project=project, pk__in=member_ids, user__kind=Kind.PERSON, user__is_active=True
        )
        .select_related("user", "project")
        .prefetch_related("lab_roles")
        .in_bulk()
    )
    if memberships.keys() != member_ids:
        raise Refused(
            "Choose active people with a direct membership in the assay project.",
            field="role_assignments",
        )
    for role, group in assignments.items():
        minimum = starter.roles[role]
        for key in group:
            member = memberships[key]
            # A person's teams can also give them the project role this work needs.
            if (
                member.role < minimum
                and (permissions.project_role(member.user, project) or 0) < minimum
            ):
                raise Refused(
                    f"{lab_roles.label(role)} requires {minimum.label} or higher.",
                    field="role_assignments",
                )
    # For these three signatures, each group of roles needs at least as many distinct
    # people as there are roles in the group (Hall's marriage theorem). Seven checks
    # suffice however many members an administrator selects.
    independent = starter.independent_roles.get(flow, ())
    if any(
        len(set().union(*(assignments[r] for r in group))) < count
        for count in range(1, len(independent) + 1)
        for group in combinations(independent, count)
    ):
        raise Refused(
            "Choose three different people who can submit, independently review and release "
            "the result.",
            field="role_assignments",
        )
    return memberships


@dataclass
class _Plan:
    starter: Starter
    application: StarterApplication | None
    members: dict[UUID, ProjectMembership]
    grants: dict[UUID, set[str]]
    result: dict[str, Any]


def _plan(
    project: Project,
    starter: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    requester_project: Project,
    by: User,
) -> _Plan:
    permissions.require(by, Action.PROJECT_MANAGE, project)
    permissions.require(by, Action.PROJECT_EDIT, requester_project)
    if project.is_archived:
        raise Refused("Restore the assay project before applying a starter.", field="project")
    recipe = installed_starters().get(starter)
    if recipe is None:
        raise Http404("This starter is not installed.")
    if version != recipe.version:
        raise Refused(
            f"The installed version of {starter} is {recipe.version}. Preview it before applying.",
            field="version",
        )
    if flow not in recipe.flows:
        raise Refused("Choose one of the starter's review flows.", field="flow")
    current = StarterApplication.objects.filter(project=project, starter=starter).first()
    if current is not None and current.requester_project_id != requester_project.pk:
        raise Refused(
            "Use the original requester project to keep its example request and results together.",
            field="requester_project",
        )
    if current is None:
        recipe.examples.check_example(project, requester_project, by=by)
    selected = {role: set(ids) for role, ids in role_assignments.items()}
    members = _members(project, recipe, flow, selected)
    held = {key: {g.role for g in m.lab_roles.all()} for key, m in members.items()}
    grants: dict[UUID, set[str]] = defaultdict(set)
    missing: dict[str, list[str]] = {}
    assignments: dict[str, list[str]] = {}
    for role, group in sorted(selected.items()):
        assignments[role] = sorted(str(pk) for pk in group)
        for pk in sorted(group):
            if role not in held[pk]:
                grants[pk].add(role)
                missing.setdefault(role, []).append(str(pk))
    return _Plan(
        recipe,
        current,
        members,
        grants,
        {
            "starter": starter,
            "version": version,
            "project": str(project.pk),
            "requester_project": str(requester_project.pk),
            "flow": flow,
            "result_type": recipe.requested_types[flow],
            "changed": current is None
            or current.version != version
            or current.flow != flow
            or bool(grants),
            "role_assignments": assignments,
            "grants_to_add": missing,
            "examples_to_create": list(recipe.example_keys) if current is None else [],
            "examples": dict(current.examples) if current is not None else {},
        },
    )


def preview(
    project: Project,
    starter: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    requester_project: Project,
    by: User,
) -> dict[str, Any]:
    return _plan(project, starter, version, flow, role_assignments, requester_project, by).result


@transaction.atomic
def apply(
    project: Project,
    starter: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    requester_project: Project,
    by: User,
) -> dict[str, Any]:
    # Both intake and example writes involve the two projects. Ordered locking also
    # serializes two first applications and a flow switch against new result records.
    locked = {
        p.pk: p
        for p in Project.objects.select_for_update()
        .filter(pk__in=[project.pk, requester_project.pk])
        .order_by("pk")
    }
    project, requester_project = locked[project.pk], locked[requester_project.pk]
    permissions.forget(by)
    plan = _plan(project, starter, version, flow, role_assignments, requester_project, by)
    result = plan.result
    if not result["changed"]:
        return result
    current = plan.application
    previous = {"version": current.version, "flow": current.flow} if current else None
    for key, roles in plan.grants.items():
        membership = plan.members[key]
        held = {g.role for g in membership.lab_roles.all()}
        services.set_lab_roles(membership, held | roles, by=by)
    if current is None:
        examples = plan.starter.examples.seed_example(project, requester_project, by=by)
        current = StarterApplication(
            project=project, requester_project=requester_project, starter=starter, examples=examples
        )
    current.version, current.flow, current.applied_by = version, flow, by
    current.save()
    result["examples"] = dict(current.examples)
    audit.record(
        "starter.applied",
        actor=by,
        project=project,
        target=current,
        data={
            "starter": starter,
            "before": previous,
            "after": {"version": version, "flow": flow},
            "role_assignments": result["role_assignments"],
            "grants_added": result["grants_to_add"],
            "requester_project": str(requester_project.pk),
            "examples": result["examples"],
        },
    )
    return result
