"""Machines Iki starts for runs, and how their workers register (ADR 0061).

The provisioner (:mod:`iki_server.runs.provisioner`) starts a container-tier pod for an
environment on a compute template, and records it as a :class:`Launch` with a token made
for it alone (``iki_wlch_…``). The pod's worker registers with that token, once, before it
expires. What the worker may run comes from the launch, not from what it says: the
environment it was started for, in the image it was started with, at the plugin commit
the runs were launched with, with the template's capacity. A launch for an ephemeral
machine gives its worker one job; then it stops.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.runs.models import Job, Launch, LaunchState, Worker
from iki_spec.compute import Template

__all__ = [
    "LAUNCH_PREFIX",
    "START_WITHIN",
    "capacity_of",
    "create",
    "end",
    "for_token",
    "may_lease",
    "register",
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
) -> tuple[Launch, str]:
    """A new launch, and its token (given to the pod only)."""
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
            token_hash=_hash(value),
            created_at=now,
            expires_at=now + START_WITHIN,
        )
        launch.name = f"iki-run-{launch.pk.hex}"
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
            tier="container",
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
                "tier": "container",
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


def may_lease(worker: Worker) -> bool:
    """Whether a launched worker may lease another job: one, on an ephemeral machine."""
    launch = worker.launch
    if launch is None or not launch.once:
        return True
    return not Job.objects.filter(worker=worker).exists()


def end(launch: Launch, reason: str, message: str = "") -> bool:
    """Marks the launch ended, and revokes its worker if it registered (its jobs are
    interrupted, and retried under their policy). Returns whether it was still going."""
    from iki_server.runs import workers

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
    return True
