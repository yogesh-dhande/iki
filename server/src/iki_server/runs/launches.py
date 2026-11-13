"""Machines Iki starts for runs, and how their workers register (ADR 0061).

The provisioner (:mod:`iki_server.runs.provisioner`) starts a container-tier pod or a
vm-tier EC2 instance (ADR 0074) for an environment on a compute template, and records it as
a :class:`Launch` with a token made for it alone (``iki_wlch_…``). The machine's worker
registers with that token, once, before it expires. What the worker may run comes from the
launch, not from what it says: the environment it was started for, in the image it was
started with, at the plugin commit the runs were launched with, with the template's
capacity. A launch for an ephemeral machine gives its worker one job; then it stops. A
persistent one runs one job after another, until the provisioner stops it while it's idle
(ADR 0062)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.runs.models import Job, Launch, LaunchState, SetupBuild, Worker
from iki_spec.compute import TIER, Template

__all__ = [
    "LAUNCH_PREFIX",
    "START_WITHIN",
    "capacity_of",
    "create",
    "end",
    "for_token",
    "lifecycle",
    "may_lease",
    "register",
    "stop_if_idle",
]

LAUNCH_PREFIX = "iki_wlch_"
#: How long a pod has to start and register: pulling a large image takes a while.
START_WITHIN = timedelta(hours=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def image_digest(image: str) -> str:
    """``sha256:…`` from an image pinned by its digest."""
    return image.rpartition("@")[2]


def create(
    *,
    template: str,
    provisioner: str,
    environment: str,
    plugin_sha: str,
    image: str,
    namespace: str,
    once: bool,
    spec: str = "",
    setup: SetupBuild | None = None,
    volumes: dict[str, Any] | None = None,
) -> tuple[Launch, str]:
    """A new launch, and its token (given to the pod only). With `setup`, its machine runs
    that environment setup build, and nothing else (ADR 0075); `volumes` are the versions
    of the environment's volumes it gets."""
    value = LAUNCH_PREFIX + secrets.token_urlsafe(32)
    now = timezone.now()
    with transaction.atomic():
        launch = Launch(
            template=template,
            provisioner=provisioner,
            environment=environment,
            plugin_sha=plugin_sha,
            image=image,
            namespace=namespace,
            once=once,
            spec=spec,
            setup=setup,
            volumes=volumes or {},
            token_hash=_hash(value),
            created_at=now,
            expires_at=now + START_WITHIN,
        )
        launch.name = f"iki-{'setup' if setup is not None else 'run'}-{launch.pk.hex}"
        launch.save(force_insert=True)
        audit.record(
            "worker.launched",
            target=launch,
            data={
                "template": template,
                "provisioner": provisioner,
                "environment": environment,
                "plugin_sha": plugin_sha,
                "image": image,
                "namespace": namespace,
                "name": launch.name,
                "lifecycle": "ephemeral" if once else "persistent",
                **({"setup": setup.hrid} if setup is not None else {}),
                **({"volumes": volumes} if volumes else {}),
            },
        )
    return launch, value


def for_token(token: str) -> Launch | None:
    """The launch a token is for, if its worker may still register with it."""
    if not token.startswith(LAUNCH_PREFIX) or len(token) > 200:
        return None
    launch = Launch.objects.filter(token_hash=_hash(token), state=LaunchState.STARTING).first()
    if launch is None or launch.expires_at <= timezone.now():
        return None
    return launch


def register(
    launch: Launch,
    *,
    name: str,
    version: str,
    capacity: dict[str, Any],
    template_capacity: dict[str, Any],
) -> tuple[Worker, str]:
    """The launch's worker, and its own token (shown once). Registering spends the launch's
    token. The worker gets the environment it was launched for and the template's
    capacity, whatever it says it has."""
    from iki_server.runs import workers

    value = workers.new_worker_token()
    now = timezone.now()
    plugin = launch.environment.partition(".")
    with transaction.atomic():
        locked = Launch.objects.select_for_update().get(pk=launch.pk)
        if locked.state != LaunchState.STARTING:
            raise Launch.DoesNotExist
        worker = Worker.objects.create(
            name=name,
            tier=TIER.get(locked.provisioner, "container"),
            token_hash=_hash(value),
            hint=value[-4:],
            launch=locked,
            version=version,
            capacity=template_capacity,
            plugins=[
                {
                    "name": plugin[0],
                    "sha": locked.plugin_sha,
                    "environments": {plugin[2]: image_digest(locked.image)},
                }
            ],
            last_seen_at=now,
        )
        locked.state = LaunchState.RUNNING
        locked.registered_at = now
        locked.message = ""
        locked.save(update_fields=["state", "registered_at", "message"])
        audit.record(
            "worker.registered",
            target=worker,
            data={
                "tier": worker.tier,
                "launch": str(locked.pk),
                "version": version,
                "reported_capacity": capacity,
            },
        )
    return worker, value


def capacity_of(template: Template) -> dict[str, Any]:
    """What a machine of `template` gives one job, as workers report it."""
    have = template.capacity
    return {
        "cpu": have.cpu,
        "memory": have.memory,
        "gpu_count": have.gpu_count,
        "gpu_memory": have.gpu_memory,
        "scratch_disk": have.scratch_disk,
    }


def lifecycle(launch: Launch) -> str:
    return "ephemeral" if launch.once else "persistent"


def may_lease(worker: Worker) -> bool:
    """Whether a launched worker may lease another job: while its launch runs, and only
    one on an ephemeral machine. Call it in the lease's transaction: it locks the launch,
    so the provisioner can't stop the machine as it takes a job (:func:`stop_if_idle`)."""
    if worker.launch_id is None:
        return True
    launch = Launch.objects.select_for_update().filter(pk=worker.launch_id).first()
    if launch is None or launch.state != LaunchState.RUNNING:
        return False
    return not launch.once or not Job.objects.filter(worker=worker).exists()


def stop_if_idle(launch: Launch, reason: str, message: str) -> bool:
    """Ends a persistent machine's launch if its worker has no job, as :func:`end` does.
    Returns whether it did. A lease locks the launch too, so the worker can't take a job
    while this decides."""
    from iki_server.runs.queue import HELD

    with transaction.atomic():
        locked = Launch.objects.select_for_update().get(pk=launch.pk)
        if locked.state != LaunchState.RUNNING:
            return False
        if Job.objects.filter(worker__launch=locked, state__in=HELD).exists():
            return False
        return end(locked, reason, message)


def end(launch: Launch, reason: str, message: str = "") -> bool:
    """Marks the launch ended, and revokes its worker if it registered (its jobs are
    interrupted, and retried under their policy). Returns whether it was still going."""
    from iki_server.runs import costs, workers

    with transaction.atomic():
        locked = Launch.objects.select_for_update().get(pk=launch.pk)
        if locked.state == LaunchState.ENDED:
            return False
        locked.state = LaunchState.ENDED
        locked.ended_at = timezone.now()
        locked.ended_reason = reason
        locked.message = message[:500]
        locked.save(update_fields=["state", "ended_at", "ended_reason", "message"])
        audit.record(
            "worker.launch_ended", target=locked, data={"reason": reason, "message": message}
        )
        worker = Worker.objects.filter(launch=locked, revoked_at__isnull=True).first()
        if worker is not None:
            workers.revoke_worker(worker, by=None)
        # Once its jobs are let go: what's left of what it cost (ADR 0070).
        costs.launch_ended(locked)
    return True
