"""Recipes installed with active plugins configure projects (ADRs 0198, 0223, 0240, 0247)."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from itertools import combinations
from typing import Any
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
from iki_server.setup.models import StarterApplication


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
    roles: dict[str, ProjectRole]
    required_roles: dict[str, tuple[str, ...]]
    independent_roles: dict[str, tuple[str, ...]]
    #: What the project's choice governs, as in "New dose-response fits use Research".
    governs: str = "dose-response fits"
    #: The type whose records pin the flow when an action opens them (ADR 0240). Records
    #: made under one, such as a release case's certificate, follow its pinned flow, not
    #: the project's current choice. Without one, new results must be the chosen flow's
    #: result type.
    pinned_by: str | None = None
    #: The states each flow's records pass through, where the result type has one route
    #: per flow in a single state field. Without them, a flow is its result type's.
    flow_states: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: What each flow asks of people, where it differs from the general sentence for it.
    summaries: dict[str, str] = field(default_factory=dict)

    def summary(self, flow: str) -> str:
        return self.summaries.get(flow) or FLOW_SUMMARIES[flow]

    @property
    def types(self) -> tuple[str, ...]:
        return (self.result_base, *self.flows.values())


PLATE = "iki/plate_assays."
ANALYST, REVIEWER, QA = PLATE + "analyst", PLATE + "reviewer", PLATE + "qa"
FLOW_LABELS = {
    "author_signed": "Author sign-off",
    "research": "Research",
    "regulated_qc": "Regulated QC",
    "record_only": "Record only",
}
#: What a flow asks of people, in a sentence, unless a starter says it for its records.
FLOW_SUMMARIES = {
    "author_signed": "The author signs the record. For work the author's own signature closes.",
    "research": "A result is recorded, then one reviewer signs it off. For discovery work, "
    "where one check is enough.",
    "regulated_qc": "The analyst signs the submission, an independent reviewer signs the "
    "review, and QA signs the release: three different people, each signing in again. "
    "For GxP and QC work.",
    "record_only": "A result is kept as evidence, as it was recorded, and no one signs it. "
    "For work whose review happens elsewhere, or not at all.",
}

DOSE_RESPONSE = Starter(
    id="dose-response",
    version="2.0.0",
    label="Dose-response plate assay",
    description="Choose the review flow for new dose-response fits and assign the assay roles.",
    plugin="iki/plate_assays",
    setting="review_flow",
    result_base=PLATE + "AssayResult",
    flows={"research": PLATE + "ResearchAssayResult", "regulated_qc": PLATE + "QCAssayResult"},
    roles={
        ANALYST: ProjectRole.EDITOR,
        REVIEWER: ProjectRole.VIEWER,
        QA: ProjectRole.VIEWER,
    },
    required_roles={
        "research": (ANALYST, REVIEWER),
        "regulated_qc": (ANALYST, REVIEWER, QA),
    },
    independent_roles={"regulated_qc": (ANALYST, REVIEWER, QA)},
)

# Release testing, stability and prep verification pin their flow on the record an action
# opens: a case, a scheduled study, a verification (ADR 0240). A later choice governs only
# records opened after it, and the ones in progress finish as they began.
QC = "iki/qc_release."
QC_ANALYST, QC_REVIEWER, QC_QA = QC + "analyst", QC + "reviewer", QC + "qa"
LOT_RELEASE_TESTING = Starter(
    id="lot-release-testing",
    version="1.0.0",
    label="Lot release testing",
    description=(
        "Choose how new release cases' investigations and certificates of analysis are "
        "signed, and assign the release testing roles."
    ),
    plugin="iki/qc_release",
    setting="review_flow",
    result_base=QC + "ReleaseCertificate",
    flows={"research": QC + "ResearchCertificate", "regulated_qc": QC + "QCCertificate"},
    roles={
        QC_ANALYST: ProjectRole.EDITOR,
        QC_REVIEWER: ProjectRole.VIEWER,
        QC_QA: ProjectRole.VIEWER,
    },
    # Release Testing QA approves the specifications in either flow.
    required_roles={
        "research": (QC_REVIEWER, QC_QA),
        "regulated_qc": (QC_ANALYST, QC_REVIEWER, QC_QA),
    },
    independent_roles={"regulated_qc": (QC_ANALYST, QC_REVIEWER, QC_QA)},
    governs="release cases",
    pinned_by=QC + "ReleaseCase",
)

STABILITY = "iki/stability."
COORDINATOR, ST_ANALYST, ST_REVIEWER, ST_QA = (
    STABILITY + r for r in ("coordinator", "analyst", "reviewer", "qa")
)
STABILITY_STUDY = Starter(
    id="stability-study",
    version="1.0.0",
    label="Stability study",
    description=(
        "Choose how measurements on newly scheduled studies are reviewed, and assign who "
        "pulls, records, reviews and releases them."
    ),
    plugin="iki/stability",
    setting="review_flow",
    result_base=STABILITY + "Measurement",
    flows={"research": STABILITY + "Measurement", "regulated_qc": STABILITY + "Measurement"},
    flow_states={
        "research": ("recorded", "signed"),
        "regulated_qc": ("recorded", "submitted", "reviewed", "released"),
    },
    roles={
        COORDINATOR: ProjectRole.EDITOR,
        ST_ANALYST: ProjectRole.EDITOR,
        ST_REVIEWER: ProjectRole.VIEWER,
        ST_QA: ProjectRole.VIEWER,
    },
    required_roles={
        "research": (COORDINATOR, ST_REVIEWER),
        "regulated_qc": (COORDINATOR, ST_ANALYST, ST_REVIEWER, ST_QA),
    },
    independent_roles={"regulated_qc": (ST_ANALYST, ST_REVIEWER, ST_QA)},
    governs="scheduled studies",
    pinned_by=STABILITY + "Study",
)

LOTS = "iki/lots."
VER_ANALYST, VER_REVIEWER, LOT_QA = (
    LOTS + r for r in ("verification_analyst", "verification_reviewer", "qa")
)
PREP_VERIFICATION = Starter(
    id="plasmid-prep-verification",
    version="1.0.0",
    label="Plasmid prep verification",
    description=(
        "Choose how new verifications of physical plasmid preps are reviewed, and assign who "
        "submits, reviews and releases them."
    ),
    plugin="iki/lots",
    setting="verification_flow",
    result_base=LOTS + "PlasmidVerification",
    flows={"research": LOTS + "PlasmidVerification", "regulated_qc": LOTS + "PlasmidVerification"},
    flow_states={
        "research": ("recorded", "signed_off"),
        "regulated_qc": ("recorded", "submitted", "reviewed", "released"),
    },
    roles={
        VER_ANALYST: ProjectRole.EDITOR,
        VER_REVIEWER: ProjectRole.VIEWER,
        LOT_QA: ProjectRole.VIEWER,
    },
    required_roles={
        "research": (VER_REVIEWER,),
        "regulated_qc": (VER_ANALYST, VER_REVIEWER, LOT_QA),
    },
    independent_roles={"regulated_qc": (VER_ANALYST, VER_REVIEWER, LOT_QA)},
    governs="plasmid verifications",
    pinned_by=LOTS + "PlasmidVerification",
)
EXPERIMENT = "iki/experiments."
AUTHOR, WITNESS, EXPERIMENT_QA = (EXPERIMENT + r for r in ("author", "reviewer", "qa"))
# Experiments are created by hand, so every flow's records are one type with a route per
# flow. Each record pins its flow when its evidence is captured (ADR 0247).
EXPERIMENT_REVIEW = Starter(
    id="experiment-review",
    version="1.0.0",
    label="Experiment review",
    description="Choose how experiment records are signed in this project and assign the roles.",
    plugin="iki/experiments",
    setting="review_flow",
    result_base=EXPERIMENT + "Experiment",
    flows={
        "author_signed": EXPERIMENT + "Experiment",
        "research": EXPERIMENT + "Experiment",
        "regulated_qc": EXPERIMENT + "Experiment",
    },
    flow_states={
        "author_signed": ("recorded", "signed"),
        "research": ("recorded", "witnessed"),
        "regulated_qc": ("recorded", "submitted", "reviewed", "released"),
    },
    roles={
        AUTHOR: ProjectRole.EDITOR,
        WITNESS: ProjectRole.VIEWER,
        EXPERIMENT_QA: ProjectRole.VIEWER,
    },
    required_roles={
        "author_signed": (AUTHOR,),
        "research": (AUTHOR, WITNESS),
        "regulated_qc": (AUTHOR, WITNESS, EXPERIMENT_QA),
    },
    independent_roles={
        "research": (AUTHOR, WITNESS),
        "regulated_qc": (AUTHOR, WITNESS, EXPERIMENT_QA),
    },
    summaries={
        "author_signed": "The author captures the evidence and signs the record. A notebook "
        "entry or protocol is optional; one that's cited names its revision. For records the "
        "author's signature closes.",
        "research": "The author records the notebook entry and protocol revisions, then a "
        "witness who didn't create or edit the record signs it.",
        "regulated_qc": "The author signs the submission, an independent reviewer signs the "
        "review, and QA signs the release: three different people, each signing in again. "
        "For GxP work.",
    },
    governs="experiment captures",
    pinned_by=EXPERIMENT + "Experiment",
)
CATALOG = (
    DOSE_RESPONSE,
    LOT_RELEASE_TESTING,
    STABILITY_STUDY,
    PREP_VERIFICATION,
    EXPERIMENT_REVIEW,
)


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
        and plugins[s.plugin].recipes.get(s.id) == s.version
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


def list_starters(project: Project | None = None) -> list[dict[str, Any]]:
    from iki_server.setup import declared_starters

    declared = [
        declared_starters.describe(starter, procedure, project)
        for starter, procedure in declared_starters.declared().values()
    ]
    return [*_recipes(project), *declared]


def _recipes(project: Project | None) -> list[dict[str, Any]]:
    current = (
        {a.starter: a for a in StarterApplication.objects.filter(project=project)}
        if project
        else {}
    )
    labels = lab_roles.labels()
    return [
        {
            "id": s.id,
            "version": s.version,
            "label": s.label,
            "description": s.description,
            "governs": s.governs,
            "flows": [
                {
                    "id": f,
                    "label": FLOW_LABELS[f],
                    "result_type": t,
                    "states": list(s.flow_states[f]) if f in s.flow_states else None,
                    "required_roles": list(s.required_roles[f]),
                    "independent_roles": list(s.independent_roles.get(f, ())),
                    "summary": s.summary(f),
                    "steps": [],
                    "apart": [],
                }
                for f, t in s.flows.items()
            ],
            "types": list(s.types),
            "roles": [
                {"id": r, "label": labels[r], "minimum_project_role": minimum.name.lower()}
                for r, minimum in s.roles.items()
            ],
            "applied": {"version": a.version, "flow": a.flow} if (a := current.get(s.id)) else None,
            "procedure": None,
            "suggested": None,
            "slots": [],
            "chosen": None,
            "default": None,
        }
        for s in installed_starters().values()
    ]


RoleAssignments = Mapping[str, Sequence[UUID]]
NUMBERS = {2: "two", 3: "three"}


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
            "Choose active people with a direct membership in the project.",
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
    # For these independent signatures, each group of roles needs at least as many distinct
    # people as there are roles in the group (Hall's marriage theorem). Seven checks
    # suffice for three roles however many members an administrator selects.
    independent = starter.independent_roles.get(flow, ())
    if any(
        len(set().union(*(assignments[r] for r in group))) < count
        for count in range(1, len(independent) + 1)
        for group in combinations(independent, count)
    ):
        names = [lab_roles.label(r) for r in independent]
        raise Refused(
            f"Choose {NUMBERS.get(len(independent), str(len(independent)))} different people, "
            f"one each for {', '.join(names[:-1])} and {names[-1]}: each signs independently "
            "of the others.",
            field="role_assignments",
        )
    return memberships


class PlanChanged(Exception):  # noqa: N818 - what happened, as the API says it
    """What applying would do isn't what was previewed (ADR 0207). Nothing was changed;
    ``plan`` is what applying would do now, to review instead."""

    def __init__(self, plan: dict[str, Any]) -> None:
        super().__init__(
            "What applying does changed since it was previewed. Review what it does now, "
            "then apply again."
        )
        self.plan = plan


def _identify(result: Mapping[str, Any], before: Mapping[str, str] | None) -> str:
    """Names what applying does: the choice, what's set up already, and every change."""
    shown = {**result, "before": before}
    text = json.dumps(shown, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class _Plan:
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
    by: User,
) -> _Plan:
    permissions.require(by, Action.PROJECT_MANAGE, project)
    if project.is_archived:
        raise Refused("Restore the project before applying a starter.", field="project")
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
    result: dict[str, Any] = {
        "starter": starter,
        "version": version,
        "project": str(project.pk),
        "flow": flow,
        "result_type": recipe.flows[flow],
        "changed": current is None
        or current.version != version
        or current.flow != flow
        or bool(grants),
        "role_assignments": assignments,
        "grants_to_add": missing,
    }
    before = {"version": current.version, "flow": current.flow} if current is not None else None
    result["plan"] = _identify(result, before)
    return _Plan(current, members, grants, result)


def preview(
    project: Project,
    starter: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    by: User,
) -> dict[str, Any]:
    return _plan(project, starter, version, flow, role_assignments, by).result


@transaction.atomic
def apply(
    project: Project,
    starter: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    by: User,
    plan: str,
) -> dict[str, Any]:
    """Applies the plan a preview returned, identified by its ``plan``. If what applying
    does has changed since (another Admin changed a grant, or applied it first), raises
    :class:`PlanChanged` before changing anything (ADR 0207). Applying a setup that's in
    place already changes nothing, whatever the plan."""
    # Role changes, other applications and new result records take this project's lock.
    project = Project.objects.select_for_update().get(pk=project.pk)
    permissions.forget(by)
    planned = _plan(project, starter, version, flow, role_assignments, by)
    result = planned.result
    if not result["changed"]:
        return result
    if result["plan"] != plan:
        raise PlanChanged(result)
    current = planned.application
    previous = {"version": current.version, "flow": current.flow} if current else None
    for key, roles in planned.grants.items():
        membership = planned.members[key]
        held = {g.role for g in membership.lab_roles.all()}
        services.set_lab_roles(membership, held | roles, by=by)
    if current is None:
        current = StarterApplication(project=project, starter=starter)
    current.version, current.flow, current.applied_by = version, flow, by
    current.save()
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
            "plan": result["plan"],
        },
    )
    return result
