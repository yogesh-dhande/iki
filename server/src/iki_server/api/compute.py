"""Compute admin: what compute is doing now, the tokens workers register with, the workers
themselves (ADR 0043), and quotas on runs (ADR 0045).

All of it takes the Compute admin role. A registration token's value is
shown once, when it's made; only a hash is kept. Self-hosted workers belong to
teams, perhaps only some of their projects, and carry labels (ADR 0112): their
tokens say which, and an admin changes them on each worker. A Slurm cluster's worker, on its
login node, registers with a token for the cluster, and each project's runs there are
charged to the Slurm account an admin sets for it (ADR 0133).
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Prefetch, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import SCOPE_ORDER, by_scope, caller, require
from iki_server.api.pagination import PageParams, page, page_by, page_sorted
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.org.models import Project, Team
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs import catalog, overview, queue, slurm, workers
from iki_server.runs.models import (
    MAX_LABELS,
    TIERS,
    ActionRun,
    Job,
    RegistrationToken,
    RunQuota,
    RunStatus,
    SlurmAccount,
    Worker,
)
from iki_server.runs.queue import HELD
from iki_spec import releases
from iki_spec.compute import ImageBinding, Template

router = Router(tags=["compute"])

#: A worker not heard from for this long is shown as offline.
OFFLINE_AFTER = workers.OFFLINE_AFTER


# --- Overview ------------------------------------------------------------------------------


class QueueOut(Schema):
    tier: str
    environment: str = Field(description="plugin.environment")
    awaiting_approval: int = Field(description="Waiting for a person's approval (ADR 0013).")
    queued: int
    running: int
    oldest_queued_at: datetime | None = Field(
        description="When the longest-waiting queued attempt was queued."
    )
    waiting_seconds: int = Field(description="How long it has waited; 0 when none is queued.")


class WorkersOut(Schema):
    tier: str
    busy: int = Field(description="Online and holding a job.")
    idle: int = Field(description="Online with nothing to do.")
    offline: int = Field(description="Not heard from for two minutes; not revoked.")


class MachinesOut(Schema):
    template: str
    provisioner: str
    starting: int
    running: int


class OverviewOut(Schema):
    queues: list[QueueOut]
    workers: list[WorkersOut]
    machines: list[MachinesOut]
    as_of: datetime


@router.get("/compute/overview", response=OverviewOut, summary="What compute is doing now")
def compute_overview(request: HttpRequest) -> dict[str, Any]:
    """Each tier and environment's runs waiting and running, every tier's workers, and the
    machines Iki started that haven't ended. Iki exports the same numbers as metrics."""
    require(caller(request), Action.COMPUTE_MANAGE)
    now = timezone.now()
    found = overview.overview(now)
    return {
        "queues": [
            {**vars(q), "waiting_seconds": int(q.waiting(now).total_seconds())}
            for q in found.queues
        ],
        "workers": [vars(w) for w in found.workers],
        "machines": [vars(m) for m in found.machines],
        "as_of": now,
    }


class ImageBindingOut(Schema):
    plugin_version: str = Field(description="The plugin release range the image applies to.")
    image: str = Field(description="The deployment-supplied image pinned by digest.")
    pull_secret: str | None = Field(description="The name of a Kubernetes pull Secret, if set.")


class EnvironmentImageOut(Schema):
    environment: str = Field(description="The qualified plugin environment.")
    label: str
    plugin: str
    plugin_title: str
    installed_version: str = Field(description="The installed plugin version.")
    binding: ImageBindingOut | None = Field(
        description="The binding the installed version matches. Without one, the "
        "environment's container and VM runs are unavailable."
    )


#: Why no run uses an image binding.
UnusedReason = Literal["other_release", "not_installed", "not_requested", "overlapping"]


class UnusedImageBindingOut(ImageBindingOut):
    environment: str = Field(description="The qualified plugin environment it names.")
    installed_version: str | None = Field(
        description="The installed version of the environment's plugin, if it's installed."
    )
    reason: UnusedReason = Field(
        description="Why no run uses it: the installed release is outside its range; no "
        "installed plugin has the environment; the environment doesn't ask for an image; or "
        "another binding matches the same release too."
    )


class ImageBindingsOut(Schema):
    environments: list[EnvironmentImageOut] = Field(
        description="Each installed environment that needs a deployment-supplied image."
    )
    unused: list[UnusedImageBindingOut] = Field(
        description="Bindings in compute.yaml that no installed environment uses."
    )


@router.get(
    "/compute/image-bindings",
    response=ImageBindingsOut,
    summary="Deployment-supplied environment images",
)
def list_image_bindings(request: HttpRequest) -> ImageBindingsOut:
    """Each installed environment that needs an image from this deployment, and the binding
    in compute.yaml its plugin's installed version matches; and the bindings that none
    matches, so stale pins show. Pull Secrets are named; their values stay in the cluster."""
    require(caller(request), Action.COMPUTE_MANAGE)
    deployment = catalog.compute()
    plugins = registry.plugins()
    environments = []
    used: set[ImageBinding] = set()
    for plugin_name, plugin in sorted(plugins.items()):
        for name, environment in sorted(plugin.environments.items()):
            if not environment.customer_image_required:
                continue
            qualified = f"{plugin_name}.{name}"
            binding = deployment.image_binding(qualified, plugin.version)
            if binding is not None:
                used.add(binding)
            environments.append(
                EnvironmentImageOut(
                    environment=qualified,
                    label=environment.label or name,
                    plugin=plugin_name,
                    plugin_title=plugin.title or plugin_name,
                    installed_version=plugin.version,
                    binding=_image_binding_out(binding) if binding else None,
                )
            )
    unused = []
    for binding in sorted(
        deployment.image_bindings, key=lambda binding: (binding.environment, binding.plugin_version)
    ):
        if binding in used:
            continue
        owner_name, _, named = binding.environment.partition(".")
        owner = plugins.get(owner_name)
        wants = owner.environments.get(named) if owner is not None else None
        reason: UnusedReason
        if owner is None or wants is None:
            reason = "not_installed"
        elif not wants.customer_image_required:
            reason = "not_requested"
        elif releases.fits(binding.plugin_version, version=owner.version):
            reason = "overlapping"
        else:
            reason = "other_release"
        unused.append(
            UnusedImageBindingOut(
                environment=binding.environment,
                installed_version=owner.version if owner is not None else None,
                reason=reason,
                plugin_version=binding.plugin_version,
                image=binding.image,
                pull_secret=binding.pull_secret,
            )
        )
    return ImageBindingsOut(environments=environments, unused=unused)


def _image_binding_out(binding: ImageBinding) -> ImageBindingOut:
    return ImageBindingOut(
        plugin_version=binding.plugin_version, image=binding.image, pull_secret=binding.pull_secret
    )


Label = Annotated[str, Field(min_length=1, max_length=50)]
Labels = Annotated[
    list[Label],
    Field(
        max_length=MAX_LABELS,
        description="Lowercase letters, digits, dots, dashes and underscores, such as "
        "`chromeleon` or `lab-3`.",
    ),
]


class ScopeIn(Schema):
    teams: list[UUID] = Field(
        default_factory=list,
        max_length=100,
        description="Self-hosted: the teams whose runs its workers take. At least one.",
    )
    projects: list[UUID] = Field(
        default_factory=list,
        max_length=500,
        description="Self-hosted: within a team, only these of its projects. A team none of "
        "whose projects is listed is taken whole.",
    )


class WorkerRegistrationIn(ScopeIn):
    label: str = Field(
        min_length=1, max_length=100, description="What it's for, such as `GPU pool (us-east-1)`."
    )
    tier: Literal["self_hosted", "backend", "container", "vm"]
    expires_at: datetime | None = Field(
        None, description="When it stops working. Workers already registered keep working."
    )
    labels: Labels = Field(
        default_factory=list,
        description="Self-hosted: the labels the machines it registers get, which runs are "
        "matched to them by.",
    )
    slurm_cluster: str = Field(
        "",
        max_length=63,
        pattern=r"^([a-z][a-z0-9]*(-[a-z0-9]+)*)?$",
        description="Container tier: the Slurm cluster whose login node its worker runs on, "
        "as the deployment's Slurm templates name it.",
    )


class ScopeOut(Schema):
    id: UUID
    name: str


class ProjectScopeOut(Schema):
    id: UUID
    name: str
    team: ScopeOut


class WorkerRegistrationOut(Schema):
    id: UUID
    label: str
    tier: str
    teams: list[ScopeOut] = Field(description="Self-hosted: whose runs its workers take.")
    projects: list[ProjectScopeOut] = Field(
        description="Self-hosted: within a team, only these of its projects."
    )
    labels: list[str] = Field(description="Self-hosted: the labels its workers get.")
    slurm_cluster: str = Field(description="The Slurm cluster its workers submit jobs to.")
    hint: str = Field(description="The token's last four characters, to tell it by.")
    created_by: PersonRef | None = Field(
        description="Null when an operator made it with `iki-server create_registration_token`."
    )
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    workers: int = Field(description="Workers registered with it and not revoked.")


class WorkerRegistrationCreatedOut(WorkerRegistrationOut):
    token: str = Field(description="Shown once. Give it to the tier's workers as a secret.")


def _teams_out(teams: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - prefetched teams
    return [{"id": t.pk, "name": t.name} for t in sorted(teams, key=lambda t: (t.name, t.pk))]


def _projects_out(projects: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - prefetched
    return [
        {"id": p.pk, "name": p.name, "team": {"id": p.team.pk, "name": p.team.name}}
        for p in sorted(projects, key=lambda p: (p.team.name, p.name, p.pk))
    ]


def _registration_out(token: RegistrationToken) -> dict[str, Any]:
    return {
        "id": token.pk,
        "label": token.label,
        "tier": token.tier,
        "teams": _teams_out(token.teams.all()),
        "projects": _projects_out(token.projects.all()),
        "labels": token.labels,
        "slurm_cluster": token.slurm_cluster,
        "hint": token.hint,
        "created_by": token.created_by,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
        "workers": getattr(token, "live_workers", 0),
    }


def _registrations() -> QuerySet[RegistrationToken]:
    return (
        RegistrationToken.objects.select_related("created_by")
        .prefetch_related("teams", "projects__team")
        .annotate(live_workers=Count("workers", filter=Q(workers__revoked_at__isnull=True)))
    )


def _scope(payload: ScopeIn) -> workers.Scope:
    return workers.make_scope(payload.teams, payload.projects)


@router.get(
    "/compute/registration-tokens",
    response=Page[WorkerRegistrationOut],
    summary="Worker registration tokens",
)
def list_registrations(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first, revoked ones included."""
    require(caller(request), Action.COMPUTE_MANAGE)
    result = page(_registrations(), params, newest_first=True)
    result["items"] = [_registration_out(t) for t in result["items"]]
    return result


@router.post(
    "/compute/registration-tokens",
    response={201: WorkerRegistrationCreatedOut},
    summary="Make a worker registration token",
)
def create_registration(
    request: HttpRequest, payload: WorkerRegistrationIn, response: HttpResponse
) -> Any:  # noqa: ANN401
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    if payload.expires_at is not None and payload.expires_at <= timezone.now():
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/expires_at", "detail": "Choose a time in the future."}],
        )
    hosted = payload.tier == workers.SELF_HOSTED
    if not hosted and (payload.teams or payload.projects):
        raise Refused("Only self-hosted workers belong to teams.", field="teams")
    token, value = workers.create_registration(
        label=payload.label,
        tier=payload.tier,
        by=me,
        expires_at=payload.expires_at,
        scope=_scope(payload) if hosted else None,
        labels=workers.clean_labels(payload.labels),
        slurm_cluster=payload.slurm_cluster,
    )
    response["Location"] = f"/api/v1/compute/registration-tokens/{token.pk}"
    response["Cache-Control"] = "no-store"
    created = _registrations().get(pk=token.pk)
    return Status(201, {**_registration_out(created), "token": value})


class RevokeIn(Schema):
    workers: bool = Field(False, description="Also revoke the workers that registered with it.")


def _registration(token_id: UUID) -> RegistrationToken:
    found = _registrations().filter(pk=token_id).first()
    if found is None:
        raise Problem(404, "No such registration token.")
    return found


@router.post(
    "/compute/registration-tokens/{token_id}/revoke",
    response=WorkerRegistrationOut,
    summary="Revoke a worker registration token",
)
def revoke_registration(request: HttpRequest, token_id: UUID, payload: RevokeIn) -> dict[str, Any]:
    """New workers can't register with it. Workers that did keep working, unless you also
    revoke them (`workers: true`, for a token that leaked); their jobs are then interrupted,
    and retried on other workers if their action allows."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    workers.revoke_registration(_registration(token_id), by=me, workers=payload.workers)
    return _registration_out(_registration(token_id))


class RunningOut(Schema):
    id: UUID
    hrid: str
    action: str
    project: ScopeOut


class WorkerOut(Schema):
    id: UUID
    name: str
    tier: str
    hint: str
    version: str
    capacity: dict[str, Any] = Field(
        description="What it can give one job: cpu, memory, gpu_count, gpu_memory and "
        "scratch_disk (bytes)."
    )
    plugins: list[dict[str, Any]] = Field(
        description="The plugin code it has, which is all it leases: each one's `name`, "
        "`sha` (null for a local directory) and `environments` it has built, with each "
        "build's digest (null when it runs them with its host's own software)."
    )
    registration: UUID | None = Field(alias="registration_id")
    teams: list[ScopeOut] = Field(description="Self-hosted: whose runs it takes.")
    projects: list[ProjectScopeOut] = Field(
        description="Self-hosted: within a team, only these of its projects."
    )
    labels: list[str] = Field(description="Self-hosted: what runs are matched to it by.")
    slurm_cluster: str = Field(description="The Slurm cluster it submits jobs to.")
    registered_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    status: Literal["online", "offline", "revoked"]
    jobs: int = Field(description="Jobs it holds now.")
    running: list[RunningOut] = Field(description="The runs it's running now.")


def _worker_out(worker: Worker) -> dict[str, Any]:
    if worker.revoked_at is not None:
        status = "revoked"
    elif timezone.now() - worker.last_seen_at > OFFLINE_AFTER:
        status = "offline"
    else:
        status = "online"
    return {
        **{
            f: getattr(worker, f)
            for f in (
                "id",
                "name",
                "tier",
                "hint",
                "version",
                "capacity",
                "plugins",
                "registration_id",
                "slurm_cluster",
                "registered_at",
                "last_seen_at",
                "revoked_at",
            )
        },
        "teams": _teams_out(worker.teams.all()),
        "projects": _projects_out(worker.projects.all()),
        "labels": worker.labels,
        "status": status,
        "jobs": getattr(worker, "held_jobs", 0),
        "running": [
            {
                "id": job.run.pk,
                "hrid": job.run.hrid,
                "action": job.run.action,
                "project": {"id": job.project.pk, "name": job.project.name},
            }
            for job in getattr(worker, "holding", [])
        ],
    }


def _workers() -> QuerySet[Worker]:
    return Worker.objects.prefetch_related(
        "teams",
        "projects__team",
        Prefetch(
            "jobs",
            queryset=Job.objects.filter(state__in=HELD)
            .select_related("run", "project")
            .order_by("started_at", "id"),
            to_attr="holding",
        ),
    ).annotate(held_jobs=Count("jobs", filter=Q(jobs__state__in=HELD)))


class WorkerFilters(PageParams):
    tier: Literal["self_hosted", "backend", "container", "vm"] | None = None
    revoked: bool = Field(False, description="Include revoked workers.")


@router.get("/compute/workers", response=Page[WorkerOut], summary="Workers")
def list_workers(request: HttpRequest, filters: Query[WorkerFilters]) -> dict[str, Any]:
    """Newest first."""
    require(caller(request), Action.COMPUTE_MANAGE)
    found = _workers()
    if filters.tier is not None:
        found = found.filter(tier=filters.tier)
    if not filters.revoked:
        found = found.filter(revoked_at__isnull=True)
    result = page(found, filters, newest_first=True)
    result["items"] = [_worker_out(w) for w in result["items"]]
    return result


def _worker(worker_id: UUID) -> Worker:
    found = _workers().filter(pk=worker_id).first()
    if found is None:
        raise Problem(404, "No such worker.")
    return found


class WorkerPatch(Schema):
    labels: Labels | None = Field(None, description="Its labels, all of them.")
    teams: list[UUID] | None = Field(
        None, max_length=100, description="Whose runs it takes, all of them."
    )
    projects: list[UUID] | None = Field(
        None, max_length=500, description="Within a team, only these of its projects."
    )


@router.patch(
    "/compute/workers/{worker_id}", response=WorkerOut, summary="Change a self-hosted worker"
)
def change_worker(request: HttpRequest, worker_id: UUID, payload: WorkerPatch) -> dict[str, Any]:
    """Its labels, teams or projects; what's left out stays as it is. Projects left out of
    a new `teams` are dropped. Jobs it holds in projects it no longer covers are
    interrupted, and retried on other workers if their action allows. Audited."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    worker = _worker(worker_id)
    scope = None
    if payload.teams is not None or payload.projects is not None:
        teams = payload.teams
        if teams is None:
            teams = [t.pk for t in worker.teams.all()]
        scope = workers.make_scope(teams, payload.projects or [])
    labels = workers.clean_labels(payload.labels) if payload.labels is not None else None
    workers.change(worker, by=me, labels=labels, scope=scope)
    return _worker_out(_worker(worker_id))


@router.post("/compute/workers/{worker_id}/revoke", response=WorkerOut, summary="Revoke a worker")
def revoke_worker(request: HttpRequest, worker_id: UUID) -> dict[str, Any]:
    """Its token stops working at once. Jobs it holds are interrupted, and retried on other
    workers if their action allows."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    workers.revoke_worker(_worker(worker_id), by=me)
    return _worker_out(_worker(worker_id))


# --- Quotas --------------------------------------------------------------------------------------

Tier = Literal["self_hosted", "backend", "container", "vm"]


class QuotaOut(Schema):
    id: UUID
    team: ScopeOut | None
    project: ScopeOut | None
    tier: Tier | None = Field(description="None: every tier together.")
    max_running: int | None = Field(description="Jobs running at once. 0 pauses the scope.")
    max_queued: int | None = Field(description="Runs waiting to start.")
    running: int = Field(description="Jobs running in its scope now.")
    queued: int = Field(description="Runs waiting in its scope now.")
    created_by: PersonRef
    created_at: datetime
    updated_at: datetime


class QuotaIn(Schema):
    team: UUID | None = Field(
        None, description="A team's runs. Leave out team and project for the organization's."
    )
    project: UUID | None = Field(None, description="A project's runs.")
    tier: Tier | None = Field(None, description="Only this tier's runs; all tiers if left out.")
    max_running: int | None = Field(None, ge=0, le=100_000)
    max_queued: int | None = Field(None, ge=0, le=1_000_000)


class QuotaPatch(Schema):
    max_running: int | None = Field(None, ge=0, le=100_000)
    max_queued: int | None = Field(None, ge=0, le=1_000_000)


def _quota_out(quota: RunQuota) -> dict[str, Any]:
    held = Job.objects.filter(state__in=HELD)
    waiting = ActionRun.objects.filter(status=RunStatus.QUEUED)
    if quota.tier:
        held, waiting = held.filter(tier=quota.tier), waiting.filter(tier=quota.tier)
    if quota.project_id is not None:
        held, waiting = (
            held.filter(project_id=quota.project_id),
            waiting.filter(project_id=quota.project_id),
        )
    elif quota.team_id is not None:
        held, waiting = (
            held.filter(project__team_id=quota.team_id),
            waiting.filter(project__team_id=quota.team_id),
        )
    return {
        "id": quota.pk,
        "team": {"id": quota.team.pk, "name": quota.team.name} if quota.team else None,
        "project": (
            {"id": quota.project.pk, "name": quota.project.name} if quota.project else None
        ),
        "tier": quota.tier,
        "max_running": quota.max_running,
        "max_queued": quota.max_queued,
        "running": held.count(),
        "queued": waiting.count(),
        "created_by": quota.created_by,
        "created_at": quota.created_at,
        "updated_at": quota.updated_at,
    }


def _quotas() -> QuerySet[RunQuota]:
    return RunQuota.objects.select_related("team", "project", "created_by")


def _quota(quota_id: UUID) -> RunQuota:
    found = _quotas().filter(pk=quota_id).first()
    if found is None:
        raise Problem(404, "No such quota.")
    return found


def _limits_given(max_running: int | None, max_queued: int | None) -> None:
    if max_running is None and max_queued is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/max_running", "detail": "Set max_running, max_queued or both."}],
        )


class QuotaScopeOut(Schema):
    id: UUID
    name: str
    team: str | None = Field(description="A project's team; null for a team.")
    team_id: UUID | None = Field(description="A project's team's ID; null for a team.")


class ScopeSearch(PageParams):
    kind: Literal["team", "project"]
    q: str = Field("", max_length=100, description="Part of the name.")
    team: list[UUID] = Field(
        default_factory=list, description="Only projects of these teams, when given."
    )


@router.get(
    "/compute/quota-scopes", response=Page[QuotaScopeOut], summary="Teams or projects to choose"
)
def quota_scopes(request: HttpRequest, params: Query[ScopeSearch]) -> dict[str, Any]:
    """The teams or projects with `q` in their names, by name, archived ones left out.
    Compute Admins set quotas on any of them, member or not, and give self-hosted workers
    to them, so this gives names only."""
    require(caller(request), Action.COMPUTE_MANAGE)
    term = params.q.strip()
    if params.kind == "team":
        teams = Team.objects.filter(archived_at__isnull=True, name__icontains=term)
        found = page_by(teams, params, by=("name", "id"))
        return {
            **found,
            "items": [
                {"id": t.pk, "name": t.name, "team": None, "team_id": None} for t in found["items"]
            ],
        }
    projects = Project.objects.filter(
        archived_at__isnull=True, team__archived_at__isnull=True, name__icontains=term
    ).select_related("team")
    if params.team:
        projects = projects.filter(team__in=params.team)
    found = page_by(projects, params, by=("name", "team__name", "id"))
    return {
        **found,
        "items": [
            {"id": p.pk, "name": p.name, "team": p.team.name, "team_id": p.team_id}
            for p in found["items"]
        ],
    }


@router.get("/compute/quotas", response=Page[QuotaOut], summary="Run quotas")
def list_quotas(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Every quota, the organization's first, then teams', then projects', with how much of
    each is in use."""
    require(caller(request), Action.COMPUTE_MANAGE)
    found = page_by(by_scope(_quotas()), params, by=(*SCOPE_ORDER, "tier", "id"))
    return {**found, "items": [_quota_out(q) for q in found["items"]]}


@router.post("/compute/quotas", response={201: QuotaOut}, summary="Set a run quota")
def create_quota(request: HttpRequest, payload: QuotaIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """One per scope and tier. Lowering a limit doesn't stop what's running or cancel what's
    waiting; it holds back what's next."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    _limits_given(payload.max_running, payload.max_queued)
    if payload.team is not None and payload.project is not None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/project", "detail": "Choose a team or a project, not both."}],
        )
    team = Team.objects.filter(pk=payload.team).first() if payload.team else None
    project = Project.objects.filter(pk=payload.project).first() if payload.project else None
    if payload.team is not None and team is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/team", "detail": "No such team."}],
        )
    if payload.project is not None and project is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/project", "detail": "No such project."}],
        )
    if RunQuota.objects.filter(team=team, project=project, tier=payload.tier).exists():
        raise Refused("There's a quota for this scope and tier already: change that one.")
    with transaction.atomic():
        quota = RunQuota.objects.create(
            team=team,
            project=project,
            tier=payload.tier,
            max_running=payload.max_running,
            max_queued=payload.max_queued,
            created_by=me,
        )
        audit.record(
            "quota.created",
            actor=me,
            target=quota,
            project=project,
            team=team,
            data=_limits(quota),
        )
    response["Location"] = f"/api/v1/compute/quotas/{quota.pk}"
    return Status(201, _quota_out(_quota(quota.pk)))


def _limits(quota: RunQuota) -> dict[str, Any]:
    return {"tier": quota.tier, "max_running": quota.max_running, "max_queued": quota.max_queued}


@router.patch("/compute/quotas/{quota_id}", response=QuotaOut, summary="Change a run quota")
def update_quota(request: HttpRequest, quota_id: UUID, payload: QuotaPatch) -> dict[str, Any]:
    """Send both limits: a limit left out is removed."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    _limits_given(payload.max_running, payload.max_queued)
    with transaction.atomic():
        quota = RunQuota.objects.select_for_update().get(pk=_quota(quota_id).pk)
        before = _limits(quota)
        quota.max_running, quota.max_queued = payload.max_running, payload.max_queued
        quota.updated_at = timezone.now()
        quota.save(update_fields=["max_running", "max_queued", "updated_at"])
        audit.record(
            "quota.updated",
            actor=me,
            target=quota,
            project=quota.project,
            team=quota.team,
            data={"before": before, "after": _limits(quota)},
        )
    for tier in [quota.tier] if quota.tier else list(TIERS):
        queue.notify(tier)  # a raised limit may let jobs start
    return _quota_out(_quota(quota.pk))


@router.delete("/compute/quotas/{quota_id}", response={204: None}, summary="Remove a run quota")
def delete_quota(request: HttpRequest, quota_id: UUID) -> Any:  # noqa: ANN401
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    quota = _quota(quota_id)
    with transaction.atomic():
        audit.record(
            "quota.removed",
            actor=me,
            target=quota,
            project=quota.project,
            team=quota.team,
            data=_limits(quota),
        )
        quota.delete()
    for tier in [quota.tier] if quota.tier else list(TIERS):
        queue.notify(tier)
    return Status(204, None)


# --- Slurm clusters (ADR 0133) -------------------------------------------------------------


class SlurmTemplateOut(Schema):
    name: str
    partition: str
    qos: str
    account: str = Field(description="The account projects without their own are charged to.")
    constraint: str
    gpu_type: str
    max_walltime: int | None = Field(description="The partition's time limit, in seconds.")
    egress_proxy_url: str = Field(
        description="Where its compute nodes reach the egress proxy; without it, actions that "
        "reach the internet don't run there."
    )
    capacity: dict[str, Any] = Field(description="The most one job may ask for.")
    max_concurrent: int | None = Field(description="The most of its jobs at once.")
    price: dict[str, Any] | None = Field(
        description="What a job pays per hour for each CPU, GPU and GiB of memory it's given."
    )
    jobs: int = Field(description="Its jobs leased now: submitted, waiting or running.")


class SlurmClusterOut(Schema):
    name: str
    templates: list[SlurmTemplateOut]
    workers_online: int = Field(description="Its login nodes' workers heard from lately.")
    accounts: int = Field(description="Projects given their own account on it.")


def _slurm_template_out(template: Template, jobs: dict[str, int]) -> dict[str, Any]:
    given = template.slurm
    capacity = template.capacity
    rates = {
        "per_cpu_hour": template.price_per_cpu_hour,
        "per_gpu_hour": template.price_per_gpu_hour,
        "per_memory_gib_hour": template.price_per_memory_gib_hour,
    }
    return {
        "name": template.name,
        "partition": given["partition"],
        "qos": given.get("qos") or "",
        "account": given.get("account") or "",
        "constraint": given.get("constraint") or "",
        "gpu_type": given.get("gpu_type") or "",
        "max_walltime": given.get("max_walltime"),
        "egress_proxy_url": given.get("egress_proxy_url") or "",
        "capacity": {
            "cpu": capacity.cpu,
            "memory": capacity.memory,
            "gpu_count": capacity.gpu_count,
            "gpu_memory": capacity.gpu_memory,
            "scratch_disk": capacity.scratch_disk,
        },
        "max_concurrent": template.max_concurrent,
        "price": {**rates, "currency": template.currency} if template.currency else None,
        "jobs": jobs.get(template.name, 0),
    }


@router.get("/compute/slurm-clusters", response=Page[SlurmClusterOut], summary="Slurm clusters")
def list_slurm_clusters(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """The clusters the deployment's Slurm templates name, from its compute.yaml, which is
    reviewed like code (ADR 0041), by name: each one's templates, workers and accounts."""
    require(caller(request), Action.COMPUTE_MANAGE)
    paged = page_sorted(slurm.clusters().items(), params, by=("name",), key=lambda c: (c[0],))
    found = dict(paged["items"])
    names = [t.name for ts in found.values() for t in ts]
    jobs = dict(
        Job.objects.filter(state__in=HELD, template__in=names, packed_into__isnull=True)
        .values("template")
        .annotate(n=Count("pk"))
        .values_list("template", "n")
    )
    since = timezone.now() - OFFLINE_AFTER
    online = dict(
        Worker.objects.filter(
            slurm_cluster__in=list(found), revoked_at__isnull=True, last_seen_at__gte=since
        )
        .values("slurm_cluster")
        .annotate(n=Count("pk"))
        .values_list("slurm_cluster", "n")
    )
    accounts = dict(
        SlurmAccount.objects.filter(cluster__in=list(found))
        .values("cluster")
        .annotate(n=Count("pk"))
        .values_list("cluster", "n")
    )
    return {
        **paged,
        "items": [
            {
                "name": name,
                "templates": [_slurm_template_out(t, jobs) for t in templates],
                "workers_online": online.get(name, 0),
                "accounts": accounts.get(name, 0),
            }
            for name, templates in found.items()
        ],
    }


class SlurmAccountOut(Schema):
    cluster: str
    project: ProjectScopeOut
    account: str
    set_by: PersonRef | None
    set_at: datetime


class SlurmAccountIn(Schema):
    account: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="The Slurm account (as `sacctmgr` names it) its jobs are charged to.",
    )


def _slurm_account_out(given: SlurmAccount) -> dict[str, Any]:
    project = given.project
    return {
        "cluster": given.cluster,
        "project": {
            "id": project.pk,
            "name": project.name,
            "team": {"id": project.team.pk, "name": project.team.name},
        },
        "account": given.account,
        "set_by": given.set_by,
        "set_at": given.set_at,
    }


def _slurm_cluster(cluster: str) -> str:
    if cluster not in slurm.clusters():
        raise Problem(404, "This deployment has no such Slurm cluster.")
    return cluster


@router.get(
    "/compute/slurm-clusters/{cluster}/accounts",
    response=Page[SlurmAccountOut],
    summary="Projects' Slurm accounts",
)
def list_slurm_accounts(
    request: HttpRequest, cluster: str, params: Query[PageParams]
) -> dict[str, Any]:
    """The projects given their own account on the cluster. The rest are charged to their
    template's."""
    require(caller(request), Action.COMPUTE_MANAGE)
    found = SlurmAccount.objects.filter(cluster=_slurm_cluster(cluster)).select_related(
        "project__team", "set_by"
    )
    result = page(found, params, newest_first=True)
    result["items"] = [_slurm_account_out(a) for a in result["items"]]
    return result


def _project(project_id: UUID) -> Project:
    found = Project.objects.filter(pk=project_id).select_related("team").first()
    if found is None:
        raise Problem(404, "No such project.")
    return found


@router.put(
    "/compute/slurm-clusters/{cluster}/accounts/{project_id}",
    response=SlurmAccountOut,
    summary="Set a project's Slurm account",
)
def set_slurm_account(
    request: HttpRequest, cluster: str, project_id: UUID, payload: SlurmAccountIn
) -> dict[str, Any]:
    """Its runs on the cluster are charged to it from their next attempt; ones already
    submitted keep theirs. Audited, with the account before and after."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    project = _project(project_id)
    slurm.set_account(_slurm_cluster(cluster), project, payload.account, by=me)
    given = SlurmAccount.objects.select_related("project__team", "set_by").get(
        cluster=cluster, project=project
    )
    return _slurm_account_out(given)


@router.delete(
    "/compute/slurm-clusters/{cluster}/accounts/{project_id}",
    response={204: None},
    summary="Clear a project's Slurm account",
)
def clear_slurm_account(request: HttpRequest, cluster: str, project_id: UUID) -> Any:  # noqa: ANN401
    """Its runs on the cluster are charged to their template's account again."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    slurm.set_account(_slurm_cluster(cluster), _project(project_id), None, by=me)
    return Status(204, None)
