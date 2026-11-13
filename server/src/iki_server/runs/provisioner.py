"""The provisioner: starts machines for container- and vm-tier runs, and tidies up after
them (ADR 0061, ADR 0074). ``iki-server provision`` runs it; it's the only part of Iki that
may talk to the Kubernetes API or start cloud machines.

Each kind of machine is a :class:`Fleet`: Kubernetes pods (:class:`KubernetesFleet`, here)
for the container tier, EC2 instances (:class:`iki_server.runs.ec2_fleet.Ec2Fleet`) for the
vm tier. What to start, keep warm and stop is decided the same way for each.

Each :func:`tick`:

- **Starts** what queued runs need. Each queued container-tier job goes to the first of
  its environment's templates (by priority, then size) that it fits and whose lifecycle
  its action asks for. The provisioner starts a pod for each job that no free pod can take
  (one starting, or a persistent one with nothing to do), up to the template's
  ``max_concurrent``, a few at a time. A template whose pods keep failing to start waits
  longer each time before the next.
- **Keeps machines warm** (ADR 0062). ``k8s_deployment`` templates are persistent: their
  pods run one job after another. Each keeps ``min_warm`` running for each environment
  bound to it, and stops one that has had nothing to do for ``idle_shutdown`` (ten minutes
  unless it says) when there are more than that. One started before its template, or how
  Iki starts run pods, changed is replaced as soon as it's idle. A lease and a stop both
  lock the launch, so a machine is never stopped as it takes a job.
- **Reconciles** what Kubernetes has with what Iki launched. A launch whose Job finished,
  or was deleted, ends (its worker's jobs are interrupted and retried); a Job whose launch
  ended is deleted. A pod that can't pull its image is ended after a short grace, and
  when that happens again and again, the queued runs in that environment fail with a
  message saying so, rather than waiting forever. A pod still not running when its token
  expires is ended. While a pod waits for a node, its runs say so.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeGuard

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

from iki_server.entities import registry
from iki_server.runs import catalog, launches, queue, quotas, setups, volumes
from iki_server.runs.kubernetes import (
    KEEP_FINISHED,
    LABEL,
    RUN_LABELS,
    Cluster,
    KubernetesError,
    RunPods,
    job_manifest,
    secret_manifest,
)
from iki_server.runs.models import Job, JobState, Launch, LaunchState
from iki_spec.compute import Template
from iki_spec.model import Resources

__all__ = [
    "PROVISIONERS",
    "Fleet",
    "Group",
    "KubernetesFleet",
    "Trouble",
    "demand",
    "namespaces",
    "reconcile",
    "run_pods",
    "spec_of",
    "start",
    "stop_idle",
    "template_for",
    "tick",
    "trouble",
    "waiting",
    "warm",
]

logger = structlog.get_logger("iki.provisioner")

#: The provisioners Kubernetes pods are started for: a pod per run, or persistent pods.
PROVISIONERS = ("k8s_job", "k8s_deployment")
#: How long a persistent machine may have nothing to do before it's stopped, unless its
#: template's ``idle_shutdown`` says.
IDLE_SHUTDOWN = timedelta(minutes=10)
#: At most this many pods are started for one template and environment in a tick, and
#: this many in all, so a burst of runs doesn't flood the API server.
BURST = 10
BURST_TOTAL = 50
#: How long a pod may fail to pull its image before it's ended (a registry hiccup
#: retries within it).
IMAGE_GRACE = timedelta(minutes=2)
#: Launches that failed to start within this window count toward backing off, and this
#: many image failures in it fail the environment's queued runs.
FAILURE_WINDOW = timedelta(minutes=30)
GIVE_UP_AFTER = 3
#: The first wait after a failed launch, doubling with each, up to the most.
BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=10)
#: A Job gone this soon after its launch may not be listed yet.
LIST_LAG = timedelta(seconds=60)

#: Reasons a container waits that mean its image can't be had.
IMAGE_TROUBLE = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
#: And that it can't start for some other reason that won't go away by itself.
START_TROUBLE = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
FAILED_TO_START = ("image_unavailable", "refused", "never_started", "failed", "no_capacity")
#: How long after a machine couldn't be had for want of capacity its runs say so.
NO_CAPACITY_SHOWN = timedelta(minutes=15)
#: Reasons no pod for an environment's runs can start until someone fixes something.
HOPELESS = ("image_unavailable", "refused")


def run_pods() -> RunPods:
    return RunPods(
        server_url=settings.IKI_RUNS_SERVER_URL,
        worker_image=settings.IKI_RUNS_WORKER_IMAGE,
        service_account=settings.IKI_RUNS_SERVICE_ACCOUNT,
        allow_http=settings.IKI_RUNS_ALLOW_HTTP,
        image_pull_secrets=tuple(settings.IKI_RUNS_IMAGE_PULL_SECRETS),
    )


def namespaces() -> list[str]:
    """Where run pods may go: the default namespace, then any others the chart allows."""
    found = [settings.IKI_RUNS_NAMESPACE, *settings.IKI_RUNS_NAMESPACES]
    return list(dict.fromkeys(n for n in found if n))


def namespace_of(template: Template) -> str:
    return str(template.kubernetes.get("namespace") or settings.IKI_RUNS_NAMESPACE)


class Fleet:
    """One kind of machine the provisioner starts: what it asks its platform for, and how it
    squares what the platform reports with what Iki launched."""

    #: The tier its machines run, and the template provisioners it starts them for.
    tier = "container"
    provisioners: tuple[str, ...] = PROVISIONERS
    #: What its machines are, for people.
    kind = "run pods"

    def place(self, template: Template) -> str | None:
        """Where a template's machines go (a namespace, a region), or None if they may not."""
        raise NotImplementedError

    def spec(self, group: Group, place: str) -> str:
        """A digest of the machine a launch for `group` gets, which changes when its template,
        or how Iki starts them, does."""
        raise NotImplementedError

    def launch(self, group: Group, place: str) -> bool:
        """Starts a machine for `group`, as a new launch. Returns whether it started."""
        raise NotImplementedError

    def reconcile(self, now: datetime) -> int:
        """Brings launches and the platform into line. Returns how many launches it ended."""
        raise NotImplementedError

    def remove(self, launch: Launch) -> None:
        """Stops the launch's machine, whatever it's doing; nothing if it's gone already."""
        raise NotImplementedError

    def check(self, launch: Launch, reason: str) -> str:
        """What an administrator can check when no machine can start for an environment."""
        raise NotImplementedError


@dataclass
class Group:
    """Queued jobs that the same kind of pod can run."""

    environment: str
    plugin_sha: str
    template: Template
    image: str
    jobs: list[Job] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.environment, self.plugin_sha, self.template.name)

    @property
    def once(self) -> bool:
        return self.template.lifecycle == "ephemeral"


def spec_of(
    group: Group, namespace: str, pods: RunPods, volumes: dict[str, Any] | None = None
) -> str:
    """A digest of the pod a launch for `group` gets: it changes when its template, how
    Iki starts run pods (the worker's image, say), or the version of the environment's
    volumes it gets (ADR 0075) does."""
    job, _ = job_manifest(
        name="-",
        launch_id="-",
        template=group.template,
        environment=group.environment,
        image=group.image,
        pods=pods,
        once=group.once,
        volumes=volumes,
    )
    canonical = json.dumps([namespace, job], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _resources(job: Job) -> Resources:
    known = {k: v for k, v in (job.resources or {}).items() if k in Resources.__dataclass_fields__}
    return Resources(**known)


def template_for(
    job: Job,
    provisioners: tuple[str, ...] = PROVISIONERS,
    ready: dict[tuple[str, str], bool] | None = None,
) -> tuple[Template, str] | None:
    """Which template the provisioner starts a machine on for `job`, and the image; None if
    none it starts fits (or the action is gone). The first that fits, in the binding's
    order, where a version of the environment's volumes is ready, if any is (ADR 0077):
    runs don't wait for one place's data while another's is ready. `ready` remembers
    which places have one, for a round."""
    action = catalog.action(job.run.action)
    if action is None:
        return None
    image = catalog.environment(action).image
    if image is None:
        return None
    wanted = _resources(job)
    fitting = [
        template
        for template in catalog.availability(action).templates
        if template.provisioner in provisioners
        and template.fits(wanted)
        and (action.lifecycle is None or template.lifecycle == action.lifecycle)
    ]
    if len(fitting) > 1:
        known = ready if ready is not None else {}
        for template in fitting:
            place = setups.place_of(template)
            if place is None:
                continue
            key = (job.environment, place)
            if key not in known:
                known[key] = setups.volumes_for(job.environment, place) is not None
            if known[key]:
                return template, image
    return (fitting[0], image) if fitting else None


def demand(now: datetime, fleet: Fleet | None = None) -> dict[tuple[str, str, str], Group]:
    """The queued jobs on the fleet's tier that need a machine, by what kind of machine."""
    tier = fleet.tier if fleet is not None else "container"
    provisioners = fleet.provisioners if fleet is not None else PROVISIONERS
    stop = quotas.blocked(tier)
    if stop.organization:
        return {}
    jobs = (
        Job.objects.filter(
            state=JobState.QUEUED, tier=tier, packed_into__isnull=True, queued_at__lte=now
        )
        .exclude(project__in=list(stop.projects))
        .exclude(project__team__in=list(stop.teams))
        .select_related("run")
        .order_by("-priority", "queued_at")[:5000]
    )
    groups: dict[tuple[str, str, str], Group] = {}
    ready: dict[tuple[str, str], bool] = {}
    for job in jobs:
        chosen = template_for(job, provisioners, ready)
        if chosen is None:
            continue
        template, image = chosen
        key = (job.environment, job.run.plugin_sha, template.name)
        group = groups.get(key)
        if group is None:
            group = groups[key] = Group(job.environment, job.run.plugin_sha, template, image)
        group.jobs.append(job)
    return groups


def warm(provisioners: tuple[str, ...] = PROVISIONERS) -> dict[tuple[str, str, str], Group]:
    """The machines persistent templates keep running with nothing to do: ``min_warm`` of
    each, for each environment bound to it, at the plugin commit runs launch with now."""
    from iki_server.runs.services import plugin_sha

    deployment = catalog.compute()
    plugins = registry.plugins()
    found: dict[tuple[str, str, str], Group] = {}
    for qualified, binding in deployment.bindings.items():
        plugin, _, name = qualified.partition(".")
        environment = plugins[plugin].environments.get(name) if plugin in plugins else None
        if environment is None or environment.image is None:
            continue
        sha = plugin_sha(plugin)
        for template_name, _ in binding.templates:
            template = deployment.templates.get(template_name)
            if template is None or template.provisioner not in provisioners:
                continue
            if template.min_warm and template.lifecycle == "persistent":
                group = Group(qualified, sha, template, environment.image)
                found[group.key] = group
    return found


def _free(provisioners: tuple[str, ...]) -> list[Launch]:
    """Machines that can take a job now or once they start: one starting, or a persistent
    one with nothing to do."""
    held = Job.objects.filter(worker__launch=OuterRef("pk"), state__in=queue.HELD)
    ever = Job.objects.filter(worker__launch=OuterRef("pk"))
    return list(
        Launch.objects.exclude(state=LaunchState.ENDED)
        # A machine started for a setup build runs nothing else (ADR 0075).
        .filter(provisioner__in=provisioners, setup__isnull=True)
        .annotate(held=Exists(held), ever=Exists(ever))
        .filter(Q(once=False, held=False) | Q(once=True, ever=False))
        .order_by("created_at")
    )


def _serves(launch: Launch, job: Job, templates: dict[str, Template]) -> bool:
    """Whether `launch`'s worker may lease `job`, as :func:`queue.lease` decides."""
    template = templates.get(launch.template)
    return (
        template is not None
        and launch.environment == job.environment
        and launch.plugin_sha == job.run.plugin_sha
        and job.lifecycle in ("", launches.lifecycle(launch))
        and template.fits(_resources(job))
    )


def _unmet(
    groups: dict[tuple[str, str, str], Group], provisioners: tuple[str, ...]
) -> Counter[tuple[str, str, str]]:
    """How many of each group's jobs no free machine can take."""
    templates = catalog.compute().templates
    spare = _free(provisioners)
    unmet: Counter[tuple[str, str, str]] = Counter()
    for key, group in groups.items():
        for job in group.jobs:
            taken = next((i for i, m in enumerate(spare) if _serves(m, job, templates)), None)
            if taken is None:
                unmet[key] += 1
            else:
                del spare[taken]
    return unmet


def _backing_off(group: Group, now: datetime) -> bool:
    failed = list(
        Launch.objects.filter(
            environment=group.environment,
            template=group.template.name,
            ended_reason__in=FAILED_TO_START,
            ended_at__gt=now - FAILURE_WINDOW,
        )
        .order_by("-ended_at")
        .values_list("ended_at", flat=True)[:10]
    )
    if not failed or failed[0] is None:
        return False
    last: datetime = failed[0]
    wait = min(BACKOFF * 2 ** (len(failed) - 1), MAX_BACKOFF)
    return bool(last + wait > now)


def start(
    fleet: Fleet,
    now: datetime,
    groups: dict[tuple[str, str, str], Group],
    kept: dict[tuple[str, str, str], Group],
) -> int:
    """Starts the machines queued runs need, and the warm ones persistent templates keep.
    Returns how many."""
    wanted = {**kept, **groups}
    if not wanted:
        return 0
    unmet = _unmet(groups, fleet.provisioners)
    live = Counter(
        Launch.objects.exclude(state=LaunchState.ENDED).values_list("template", flat=True)
    )
    running = Counter(
        Launch.objects.exclude(state=LaunchState.ENDED)
        .filter(setup__isnull=True)
        .values_list("environment", "plugin_sha", "template")
    )
    started = 0
    for key, group in wanted.items():
        template = group.template
        want = unmet[key]
        if key in kept:
            want = max(want, template.min_warm - running[key])
        want = min(want, BURST, BURST_TOTAL - started)
        if template.max_concurrent is not None:
            want = min(want, template.max_concurrent - live[template.name])
        if want <= 0 or _backing_off(group, now):
            continue
        place = fleet.place(template)
        if place is None:
            continue
        for _ in range(want):
            if not fleet.launch(group, place):
                break
            started += 1
            live[template.name] += 1
    return started


def _launch(cluster: Cluster, group: Group, namespace: str, pods: RunPods) -> bool:
    volumes = setups.volumes_for(group.environment, namespace)
    if volumes is None:
        # Its setup fills volumes, and no version is ready here: its runs say so.
        return False
    launch, token = launches.create(
        template=group.template.name,
        provisioner=group.template.provisioner,
        environment=group.environment,
        plugin_sha=group.plugin_sha,
        image=group.image,
        namespace=namespace,
        once=group.once,
        spec=spec_of(group, namespace, pods, volumes),
        volumes=setups.provenance(volumes),
    )
    job, secret = job_manifest(
        name=launch.name,
        launch_id=str(launch.pk),
        template=group.template,
        environment=group.environment,
        image=group.image,
        pods=pods,
        once=group.once,
        volumes=volumes,
    )
    try:
        cluster.create_job(
            namespace, job, secret_manifest(secret, str(launch.pk), token, pods.labels)
        )
    except (KubernetesError, OSError) as error:
        logger.warning("launch_failed", launch=str(launch.pk), error=str(error))
        if refused(error):
            # The cluster's admission policy said no: its image isn't signed as it asks,
            # say (ADR 0063). It will say no again until someone changes something.
            message = _clip(f"The cluster refused to start it: {error.message}")
            launches.end(launch, "refused", message)
            give_up_if_hopeless(
                KubernetesFleet(cluster), launch, "refused", message, timezone.now()
            )
        else:
            launches.end(launch, "failed", f"Kubernetes didn't start it: {error}")
        return False
    logger.info(
        "launched",
        launch=str(launch.pk),
        template=group.template.name,
        environment=group.environment,
        namespace=namespace,
    )
    return True


def refused(error: Exception) -> TypeGuard[KubernetesError]:
    """Whether Kubernetes refused an object because an admission webhook, such as an image
    signature policy, denied it."""
    return (
        isinstance(error, KubernetesError)
        and error.status in (400, 403)
        and "admission webhook" in error.message
        and "denied the request" in error.message
    )


# --- Reconciling -----------------------------------------------------------------------------


def _finished(job: dict[str, Any]) -> tuple[str, str] | None:
    """Whether a Job finished: (reason, message), or None while it runs."""
    for condition in (job.get("status") or {}).get("conditions") or []:
        if condition.get("status") != "True":
            continue
        if condition.get("type") == "Complete":
            return "finished", ""
        if condition.get("type") == "Failed":
            return "failed", str(condition.get("message") or condition.get("reason") or "")
    return None


@dataclass(frozen=True)
class Trouble:
    reason: str  # image_unavailable, failed or waiting
    message: str
    since: datetime | None = None


def trouble(pod: dict[str, Any] | None) -> Trouble | None:
    """What's keeping a pod from running, if anything."""
    if pod is None:
        return None
    status = pod.get("status") or {}
    for condition in status.get("conditions") or []:
        if (
            condition.get("type") == "PodScheduled"
            and condition.get("status") == "False"
            and condition.get("reason") == "Unschedulable"
        ):
            return Trouble(
                "waiting", _clip(f"Waiting for a machine: {condition.get('message', '')}")
            )
    statuses = [
        *(status.get("initContainerStatuses") or []),
        *(status.get("containerStatuses") or []),
    ]
    for container in statuses:
        waiting = (container.get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason")
        if reason in IMAGE_TROUBLE:
            return Trouble(
                "image_unavailable",
                _clip(f"Its image couldn't be pulled: {waiting.get('message') or reason}"),
                _started(pod),
            )
        if reason in START_TROUBLE:
            return Trouble(
                "failed",
                _clip(f"It couldn't start: {waiting.get('message') or reason}"),
                _started(pod),
            )
    return None


def _started(pod: dict[str, Any]) -> datetime | None:
    raw = (pod.get("metadata") or {}).get("creationTimestamp")
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _clip(text: str) -> str:
    return text if len(text) <= 500 else text[:497] + "..."


def _by_launch(objects: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found = {}
    for item in objects:
        launch = ((item.get("metadata") or {}).get("labels") or {}).get(LABEL)
        if launch:
            found[launch] = item
    return found


def reconcile(cluster: Cluster, now: datetime) -> int:
    """Brings launches and Kubernetes into line. Returns how many launches it ended."""
    live = list(
        Launch.objects.exclude(state=LaunchState.ENDED).filter(provisioner__in=PROVISIONERS)
    )
    ended = 0
    for namespace in sorted({*namespaces(), *(each.namespace for each in live)}):
        try:
            jobs = _by_launch(cluster.jobs(namespace))
            pods = _by_launch(cluster.pods(namespace))
        except (KubernetesError, OSError) as error:
            logger.warning("reconcile_list_failed", namespace=namespace, error=str(error))
            continue
        here = [each for each in live if each.namespace == namespace]
        for launch in here:
            pod = pods.get(str(launch.pk))
            ended += _reconcile_one(cluster, launch, jobs.get(str(launch.pk)), pod, now)
            if pod is not None:
                _label(cluster, launch, pod)
        known = {str(each.pk) for each in here}
        # A pod outlives its launch for a while: one that ran a short job may have ended
        # before a tick saw it holding it. It still says whose it was.
        gone = Launch.objects.filter(
            pk__in=[k for k in pods if k not in known and _is_uuid(k)],
            state=LaunchState.ENDED,
            ended_at__gte=now - timedelta(seconds=KEEP_FINISHED),
        )
        for launch in gone:
            _label(cluster, launch, pods[str(launch.pk)])
        for launch_id, job in jobs.items():
            if launch_id in known or _finished(job) is not None:
                continue
            still = Launch.objects.filter(pk=launch_id).exclude(state=LaunchState.ENDED).exists()
            if not still:
                _delete(cluster, namespace, job["metadata"]["name"])
    return ended


def _reconcile_one(
    cluster: Cluster,
    launch: Launch,
    job: dict[str, Any] | None,
    pod: dict[str, Any] | None,
    now: datetime,
) -> int:
    if job is None:
        if launch.created_at > now - LIST_LAG:
            return 0
        launches.end(launch, "gone", "Its Kubernetes Job was deleted.")
        return 1
    finished = _finished(job)
    if finished is not None:
        launches.end(launch, *finished)
        return 1
    problem = trouble(pod)
    if problem is not None and problem.reason in ("image_unavailable", "failed"):
        since = problem.since or launch.created_at
        if problem.reason == "failed" or now - since >= IMAGE_GRACE:
            launches.end(launch, problem.reason, problem.message)
            _delete(cluster, launch.namespace, launch.name)
            if problem.reason == "image_unavailable":
                give_up_if_hopeless(
                    KubernetesFleet(cluster), launch, problem.reason, problem.message, now
                )
            return 1
    if launch.state == LaunchState.STARTING and launch.expires_at <= now:
        message = problem.message if problem is not None else "It didn't start in time."
        launches.end(launch, "never_started", message)
        _delete(cluster, launch.namespace, launch.name)
        return 1
    message = problem.message if problem is not None else ""
    if launch.state == LaunchState.STARTING and launch.message != message:
        Launch.objects.filter(pk=launch.pk).update(message=message)
    return 0


def whose(launch: Launch) -> dict[str, str | None]:
    """The run, project and team a machine is working for, as its pod's labels: an ephemeral
    one's for all its life, a persistent one's while it holds the job."""
    jobs = Job.objects.filter(worker__launch=launch, packed_into__isnull=True)
    if not launch.once:
        jobs = jobs.filter(state__in=queue.HELD)
    job = (
        jobs.exclude(started_at=None).select_related("run__project").order_by("-started_at").first()
    )
    if job is None:
        return dict.fromkeys(RUN_LABELS)
    project = job.run.project
    return dict(
        zip(RUN_LABELS, (str(job.run_id), str(project.pk), str(project.team_id)), strict=True)
    )


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def _label(cluster: Cluster, launch: Launch, pod: dict[str, Any]) -> None:
    have = pod.get("metadata", {}).get("labels") or {}
    change = {k: v for k, v in whose(launch).items() if have.get(k) != v}
    if not change:
        return
    try:
        cluster.label_pod(launch.namespace, pod["metadata"]["name"], change)
    except (KubernetesError, OSError) as error:
        logger.warning("label_failed", launch=str(launch.pk), error=str(error))


def _delete(cluster: Cluster, namespace: str, name: str) -> None:
    try:
        cluster.delete_job(namespace, name)
    except (KubernetesError, OSError) as error:
        logger.warning("delete_failed", namespace=namespace, job=name, error=str(error))


def give_up_if_hopeless(
    fleet: Fleet, launch: Launch, reason: str, message: str, now: datetime
) -> None:
    """Fails the environment's queued runs once its image has failed, or the platform has
    refused its machines, again and again: none can start until someone fixes it."""
    failures = Launch.objects.filter(
        environment=launch.environment,
        plugin_sha=launch.plugin_sha,
        ended_reason=reason,
        ended_at__gt=now - FAILURE_WINDOW,
    ).count()
    if failures < GIVE_UP_AFTER:
        return
    # What to check comes first: what the platform said can be long.
    text = (
        f"No machine could start for it, {failures} times. An administrator can check "
        f"{fleet.check(launch, reason)}. {message}"
    )
    with transaction.atomic():
        for job in Job.objects.select_for_update(skip_locked=True).filter(
            state=JobState.QUEUED,
            tier=fleet.tier,
            environment=launch.environment,
            run__plugin_sha=launch.plugin_sha,
            packed_into__isnull=True,
        ):
            queue.fail_queued(job, "machine_unavailable", _clip(text))
    logger.warning(
        "environment_unavailable", environment=launch.environment, image=launch.image, reason=reason
    )


def waiting(environments: Iterable[str]) -> dict[str, str]:
    """What the machines being started for each environment are waiting for, for people:
    and lately, that there was no capacity to start one."""
    wanted = list(environments)
    found: dict[str, str] = {}
    for environment in wanted:
        # No machine starts until its volumes have a version (ADR 0075).
        if why := setups.missing(environment):
            found[environment] = why
    for environment, message in (
        Launch.objects.filter(environment__in=wanted, state=LaunchState.STARTING)
        .exclude(message="")
        .order_by("created_at")
        .values_list("environment", "message")
    ):
        found.setdefault(environment, message)
    for environment, message in (
        Launch.objects.filter(
            environment__in=[e for e in wanted if e not in found],
            ended_reason="no_capacity",
            ended_at__gt=timezone.now() - NO_CAPACITY_SHOWN,
        )
        .order_by("-ended_at")
        .values_list("environment", "message")
    ):
        found.setdefault(environment, message)
    return found


# --- Stopping idle machines ------------------------------------------------------------------


def _bound(environment: str, template: str) -> bool:
    binding = catalog.compute().bindings.get(environment)
    return binding is not None and any(name == template for name, _ in binding.templates)


def _minutes(wait: timedelta) -> str:
    minutes = round(wait.total_seconds() / 60)
    return "a minute" if minutes <= 1 else f"{minutes} minutes"


def stop_idle(
    fleet: Fleet,
    now: datetime,
    groups: dict[tuple[str, str, str], Group],
    kept: dict[tuple[str, str, str], Group],
) -> int:
    """Stops persistent machines with nothing to do that aren't needed: ones started before
    their template, or how Iki starts run pods, changed, and ones idle for their template's
    ``idle_shutdown`` beyond its ``min_warm``. Returns how many."""
    held = Job.objects.filter(worker__launch=OuterRef("pk"), state__in=queue.HELD)
    last = (
        Job.objects.filter(worker__launch=OuterRef("pk"), finished_at__isnull=False)
        .order_by("-finished_at")
        .values("finished_at")[:1]
    )
    idle = list(
        Launch.objects.filter(
            state=LaunchState.RUNNING, once=False, provisioner__in=fleet.provisioners
        )
        .annotate(held=Exists(held), last=Subquery(last))
        .filter(held=False)
    )
    if not idle:
        return 0
    templates = catalog.compute().templates
    running = Counter(
        Launch.objects.exclude(state=LaunchState.ENDED)
        .filter(once=False)
        .values_list("environment", "plugin_sha", "template")
    )
    # The longest idle go first.
    idle.sort(key=lambda each: each.last or each.registered_at or each.created_at)
    stopped = 0
    for launch in idle:
        key = (launch.environment, launch.plugin_sha, launch.template)
        template = templates.get(launch.template)
        since = launch.last or launch.registered_at or launch.created_at
        reason = message = ""
        if template is None or not _bound(launch.environment, template.name):
            reason, message = "replaced", "Its template is no longer bound to its environment."
        elif launch.spec != fleet.spec(
            Group(launch.environment, launch.plugin_sha, template, launch.image),
            fleet.place(template) or launch.namespace,
        ):
            reason, message = "replaced", f"Its template, or how Iki starts {fleet.kind}, changed."
        elif key not in groups:
            wait = (
                timedelta(seconds=template.idle_shutdown)
                if template.idle_shutdown is not None
                else IDLE_SHUTDOWN
            )
            keep = template.min_warm if key in kept else 0
            if now - since >= wait and running[key] > keep:
                reason, message = "idle", f"Nothing ran on it for {_minutes(wait)}."
        if reason and launches.stop_if_idle(launch, reason, message):
            fleet.remove(launch)
            running[key] -= 1
            stopped += 1
            logger.info("stopped", launch=str(launch.pk), reason=reason)
    return stopped


def tick(
    machines: Cluster | Fleet | Sequence[Fleet], now: datetime | None = None
) -> tuple[int, int]:
    """One round for each fleet: reconcile, stop idle machines that aren't needed, then
    start what is. Returns (started, ended). A Kubernetes cluster stands for its fleet."""
    now = now or timezone.now()
    if isinstance(machines, Fleet):
        fleets: Sequence[Fleet] = [machines]
    elif isinstance(machines, Sequence):
        fleets = machines
    else:
        fleets = [KubernetesFleet(machines)]
    started = ended = 0
    for fleet in fleets:
        ended += fleet.reconcile(now)
        groups = demand(now, fleet)
        kept = warm(fleet.provisioners)
        ended += stop_idle(fleet, now, groups, kept)
        started += start(fleet, now, groups, kept)
    return started, ended


class KubernetesFleet(Fleet):
    """Container-tier pods (ADR 0061, ADR 0062): a Kubernetes Job for each machine."""

    tier = "container"
    provisioners = PROVISIONERS

    def __init__(self, cluster: Cluster) -> None:
        self.cluster = cluster
        self.pods = run_pods()

    def place(self, template: Template) -> str | None:
        namespace = namespace_of(template)
        if namespace not in namespaces():
            logger.warning(
                "template_namespace_not_allowed", template=template.name, namespace=namespace
            )
            return None
        return namespace

    def spec(self, group: Group, place: str) -> str:
        return spec_of(group, place, self.pods, setups.volumes_for(group.environment, place))

    def launch(self, group: Group, place: str) -> bool:
        return _launch(self.cluster, group, place, self.pods)

    def reconcile(self, now: datetime) -> int:
        ended = reconcile(self.cluster, now)
        # Environment setup builds, and the snapshots they leave (ADR 0075).
        volumes.tick(self.cluster, self.pods, namespaces(), now)
        return ended

    def remove(self, launch: Launch) -> None:
        _delete(self.cluster, launch.namespace, launch.name)

    def check(self, launch: Launch, reason: str) -> str:
        if reason == "refused":
            return (
                f"that the deployment's build approved `{launch.image}` (it signs each image "
                "`iki deploy build --images` lists), and that the cluster's admission policy "
                "admits it"
            )
        return f"that `{launch.image}` exists and that the cluster may pull it"
