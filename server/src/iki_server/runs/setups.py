"""Environment setup builds and the volumes they fill (ADR 0075).

An environment can declare volumes its setup script fills: AlphaFold's databases, say. A
Compute admin asks for a build. If the environment's setup asks for approval, another
Compute admin gives it. The provisioner then starts a machine for the build in each
place the environment's runs go (a Kubernetes namespace), with a fresh, writable disk for
each volume. The machine's worker leases the build (not a run), runs the script with the
setup's network, and reports how it ended. The provisioner snapshots the disks, and once
every snapshot is ready, the build has succeeded: a new version of each volume.

Machines started for the environment's runs then get a read-only copy of the newest
version that isn't retired, and each job records which it read (its build, version and
``data_version``), for provenance. A retired version is no longer given to new machines;
its snapshots are deleted once no machine holds a copy.

Builds belong to the deployment, not to a project. They have their own log, egress record
and audit trail, kept as long as runs' are.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.entities import hrids, registry
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs import catalog, live
from iki_server.runs.models import (
    SETUP_HRID_PREFIX,
    SETUP_HRID_SEQUENCE,
    Launch,
    LaunchState,
    ManagedVolume,
    SetupBuild,
    SetupLog,
    SetupState,
    VolumeState,
    Worker,
)
from iki_spec.compute import KUBERNETES, Template
from iki_spec.model import Environment

__all__ = [
    "UNFINISHED",
    "Place",
    "append_log",
    "approve",
    "cancel",
    "current",
    "filled",
    "finish",
    "heartbeat",
    "lease",
    "missing",
    "places",
    "progress",
    "recover_expired",
    "request",
    "retire",
    "volumes_for",
]

UNFINISHED = (
    SetupState.AWAITING_APPROVAL,
    SetupState.QUEUED,
    SetupState.RUNNING,
    SetupState.SNAPSHOTTING,
)
#: The strategies the provisioner fills from a setup build, by provisioner.
FILLED_BY_SNAPSHOT = {"clone_from_snapshot"}
#: A lease lasts this long unless a heartbeat renews it, as for runs' jobs.
LEASE_TTL = timedelta(seconds=60)
HEARTBEAT_EVERY = timedelta(seconds=20)
#: How long past its timeout a build may run before the server ends it.
TIMEOUT_GRACE = timedelta(minutes=5)
#: The most log one build keeps.
MAX_LOG_BYTES = 16 * 1024**2
#: A setup with no timeout of its own may run this long.
DEFAULT_TIMEOUT = 6 * 3600


class Stale(Exception):  # noqa: N818 - reads as "the lease is stale"
    """The worker doesn't hold this build's current lease."""


class LogGap(Exception):  # noqa: N818 - reads as "there's a gap in the log"
    def __init__(self, expected: int) -> None:
        super().__init__(f"The log continues at byte {expected}.")
        self.expected = expected


# --- What an environment needs ---------------------------------------------------------------


def environment_of(qualified: str) -> Environment | None:
    plugin, _, name = qualified.partition(".")
    installed = registry.plugins().get(plugin)
    return installed.environments.get(name) if installed is not None else None


def filled(environment: Environment) -> list[str]:
    """The volumes the environment's setup fills, by name."""
    return sorted(n for n, v in environment.volumes.items() if v.populated_by == "setup")


@dataclass(frozen=True)
class Place:
    """Where an environment's runs go that its setup fills volumes for: a Kubernetes
    namespace, and the template a build's machine there is started on."""

    name: str
    template: Template


def _namespace(template: Template) -> str:
    from iki_server.runs.provisioner import namespace_of

    return namespace_of(template)


def places(qualified: str) -> list[Place]:
    """Each namespace the environment's bound templates start machines in, with a volume
    from a snapshot for each volume its setup fills, and the first such template there."""
    from iki_server.runs.provisioner import namespaces

    environment = environment_of(qualified)
    deployment = catalog.compute()
    binding = deployment.bindings.get(qualified)
    if environment is None or binding is None:
        return []
    wanted = filled(environment)
    allowed = set(namespaces())
    found: dict[str, Place] = {}
    for name, _ in binding.templates:
        template = deployment.templates.get(name)
        if template is None or template.provisioner not in KUBERNETES:
            continue
        given = template.volumes
        if not wanted or any(
            v not in given or given[v].strategy not in FILLED_BY_SNAPSHOT for v in wanted
        ):
            continue
        place = _namespace(template)
        if place in allowed:
            found.setdefault(place, Place(place, template))
    return list(found.values())


def current(qualified: str, place: str) -> SetupBuild | None:
    """The version machines for the environment's runs in `place` get: the newest build
    that succeeded and isn't retired."""
    return (
        SetupBuild.objects.filter(
            environment=qualified,
            place=place,
            state=SetupState.SUCCEEDED,
            retired_at__isnull=True,
        )
        .order_by("-version")
        .first()
    )


def volumes_for(qualified: str, place: str) -> dict[str, dict[str, Any]] | None:
    """What a machine for the environment's runs in `place` mounts, by volume name: the
    snapshot and how it's recorded. Empty when its setup fills none; None when it does
    but no version is ready, so no machine can start for its runs there yet."""
    environment = environment_of(qualified)
    if environment is None or not filled(environment):
        return {}
    build = current(qualified, place)
    if build is None:
        return None
    found: dict[str, dict[str, Any]] = {}
    for volume in ManagedVolume.objects.filter(build=build, state=VolumeState.READY):
        declared = environment.volumes.get(volume.name)
        if declared is None:
            continue
        found[volume.name] = {
            "build": str(build.pk),
            "hrid": build.hrid,
            "version": build.version,
            "data_version": build.data_version,
            "snapshot": volume.handle,
            "mount": declared.mount,
            "size": declared.size,
        }
    if set(found) != set(filled(environment)):
        return None
    return found


def missing(qualified: str) -> str:
    """Why machines for the environment's runs can't start for want of its volumes, for
    people; empty if they can, somewhere."""
    environment = environment_of(qualified)
    if environment is None or not filled(environment):
        return ""
    found = places(qualified)
    if any(volumes_for(qualified, place.name) is not None for place in found):
        return ""
    going = (
        SetupBuild.objects.filter(environment=qualified, state__in=UNFINISHED)
        .order_by("created_at")
        .first()
    )
    names = ", ".join(filled(environment))
    if going is not None:
        state = SetupState(going.state).label.lower()
        return f"Waiting for its reference data ({names}): setup build {going.hrid} is {state}."
    if not found:
        return (
            f"Waiting for its reference data ({names}): none of its machines can have it yet. "
            "A Compute admin can bind it to a Kubernetes template that gives its volumes "
            "from a snapshot."
        )
    return (
        f"Waiting for its reference data ({names}): no version is ready. A Compute admin "
        "can build one in Compute settings, under Volumes."
    )


def provenance(volumes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What a job records of the volumes its machine had."""
    keep = ("build", "hrid", "version", "data_version")
    return {name: {k: v[k] for k in keep if k in v} for name, v in volumes.items()}


# --- Asking for one ----------------------------------------------------------------------------


def _next_hrid() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s::regclass)", [SETUP_HRID_SEQUENCE])
        (number,) = cursor.fetchone()
    return hrids.format_hrid(SETUP_HRID_PREFIX, 6, number)


def _lock(qualified: str, place: str) -> None:
    """Serializes numbering an environment's builds in a place."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 7531))",
            [f"setup:{qualified}:{place}"],
        )


def _spec(environment: Environment) -> dict[str, Any]:
    return {
        "script": environment.setup_script,
        "timeout": environment.setup_timeout,
        "requires_approval": environment.setup_requires_approval,
        "network": list(environment.setup_network),
        "volumes": {
            name: {
                "mount": environment.volumes[name].mount,
                "size": environment.volumes[name].size,
            }
            for name in filled(environment)
        },
    }


def request(by: Any, qualified: str, reason: str = "") -> list[SetupBuild]:  # noqa: ANN401 - a User
    """Asks for a build of the environment's setup in each place its runs go. Each waits
    for approval if its setup asks for it, and is queued otherwise."""
    from iki_server.runs import queue
    from iki_server.runs.services import plugin_sha

    if not permissions.can(by, Action.COMPUTE_MANAGE):
        raise PermissionError
    environment = environment_of(qualified)
    if environment is None:
        raise LookupError(f"There's no environment {qualified}.")
    if environment.setup_script is None or not filled(environment):
        raise Refused(f"{qualified} has no setup that fills volumes.")
    if environment.image is None:
        raise Refused(f"{qualified} has no image to run its setup in.")
    found = places(qualified)
    if not found:
        raise Refused(
            f"No compute template bound to {qualified} gives its machines "
            + ", ".join(f"`{v}`" for v in filled(environment))
            + " from a snapshot (`strategy: clone_from_snapshot`), so there's nowhere to "
            "build its volumes."
        )
    plugin = qualified.partition(".")[0]
    installed = registry.plugins()[plugin]
    approval = environment.setup_requires_approval
    made: list[SetupBuild] = []
    with transaction.atomic():
        for place in found:
            _lock(qualified, place.name)
            going = SetupBuild.objects.filter(
                environment=qualified, place=place.name, state__in=UNFINISHED
            ).first()
            if going is not None:
                raise Refused(
                    f"{going.hrid} is already {SetupState(going.state).label.lower()} for "
                    f"{qualified} in {place.name}: cancel it, or wait for it to end."
                )
            latest = SetupBuild.objects.filter(environment=qualified, place=place.name).aggregate(
                n=Max("version")
            )["n"]
            build = SetupBuild.objects.create(
                hrid=_next_hrid(),
                environment=qualified,
                plugin_version=installed.version,
                plugin_sha=plugin_sha(plugin),
                image=environment.image,
                place=place.name,
                template=place.template.name,
                version=(latest or 0) + 1,
                spec=_spec(environment),
                state=SetupState.AWAITING_APPROVAL if approval else SetupState.QUEUED,
                reason=reason,
                requested_by=by,
                timeout=environment.setup_timeout or DEFAULT_TIMEOUT,
            )
            audit.record(
                "setup.requested",
                actor=by,
                target=build,
                data={
                    "hrid": build.hrid,
                    "environment": qualified,
                    "place": build.place,
                    "version": build.version,
                    "image": build.image,
                    "plugin_version": build.plugin_version,
                    "plugin_sha": build.plugin_sha,
                    "reason": reason,
                    "requires_approval": approval,
                },
            )
            made.append(build)
        if not approval:
            queue.notify("container")
    return made


def _locked(build_id: uuid.UUID) -> SetupBuild:
    found = SetupBuild.objects.select_for_update().filter(pk=build_id).first()
    if found is None:
        raise LookupError("No such setup build.")
    return found


def approve(by: Any, build_id: uuid.UUID) -> SetupBuild:  # noqa: ANN401 - a User
    """Approves a build waiting for it: someone other than who asked for it does."""
    from iki_server.runs import queue

    if not permissions.can(by, Action.COMPUTE_MANAGE):
        raise PermissionError
    with transaction.atomic():
        build = _locked(build_id)
        if build.state != SetupState.AWAITING_APPROVAL:
            raise Refused(f"{build.hrid} isn't waiting for approval.")
        if build.requested_by_id == by.pk:
            raise Refused(
                "Another Compute admin approves it: whoever asks for a setup build can't "
                "approve their own."
            )
        build.state = SetupState.QUEUED
        build.approved_by = by
        build.approved_at = timezone.now()
        build.save(update_fields=["state", "approved_by", "approved_at"])
        audit.record("setup.approved", actor=by, target=build, data={"hrid": build.hrid})
        queue.notify("container")
    return build


def cancel(by: Any, build_id: uuid.UUID) -> SetupBuild:  # noqa: ANN401 - a User
    """Cancels a build: at once if its script isn't running, and otherwise once its worker
    has stopped it."""
    if not permissions.can(by, Action.COMPUTE_MANAGE):
        raise PermissionError
    with transaction.atomic():
        build = _locked(build_id)
        if build.finished:
            raise Refused(f"{build.hrid} has ended.")
        now = timezone.now()
        build.cancel_requested_at = now
        build.cancel_requested_by = by
        fields = ["cancel_requested_at", "cancel_requested_by"]
        if build.state != SetupState.RUNNING:
            _end(build, SetupState.CANCELLED, None, now)
        build.save(update_fields=fields)
        audit.record("setup.cancelled", actor=by, target=build, data={"hrid": build.hrid})
    return build


def retire(by: Any, build_id: uuid.UUID) -> SetupBuild:  # noqa: ANN401 - a User
    """Stops giving new machines this version. Machines that have it keep it; its
    snapshots are deleted once none does."""
    if not permissions.can(by, Action.COMPUTE_MANAGE):
        raise PermissionError
    with transaction.atomic():
        build = _locked(build_id)
        if build.state != SetupState.SUCCEEDED or build.retired_at is not None:
            raise Refused(f"{build.hrid} isn't a version in use.")
        build.retired_at = timezone.now()
        build.retired_by = by
        build.save(update_fields=["retired_at", "retired_by"])
        audit.record(
            "setup.retired",
            actor=by,
            target=build,
            data={"hrid": build.hrid, "version": build.version},
        )
    return build


def _end(build: SetupBuild, state: str, error: dict[str, Any] | None, now: datetime) -> None:
    """Ends the build (saved by the caller with these fields too)."""
    build.state = state
    build.finished_at = now
    build.error = error
    build.lease_token = build.lease_expires_at = None
    build.save(update_fields=["state", "finished_at", "error", "lease_token", "lease_expires_at"])
    audit.record(
        "setup.finished",
        target=build,
        data={
            "hrid": build.hrid,
            "state": state,
            "version": build.version,
            "data_version": build.data_version,
            **({"error": error} if error else {}),
        },
    )
    live.setup_changed(build.pk)


def end(build: SetupBuild, state: str, error: dict[str, Any] | None = None) -> None:
    """Ends a build from the provisioner, in a transaction holding it."""
    _end(build, state, error, timezone.now())


# --- Its worker ----------------------------------------------------------------------------------


def lease(worker: Worker) -> tuple[SetupBuild, dict[str, Any]] | None:
    """For a worker on a machine started for a setup build: that build, if it's still to
    run, with what the worker needs to run it."""
    launch = worker.launch
    if launch is None or launch.setup_id is None:
        return None
    now = timezone.now()
    with transaction.atomic():
        locked = Launch.objects.select_for_update().filter(pk=launch.pk).first()
        if locked is None or locked.state != LaunchState.RUNNING:
            return None
        build = _locked(launch.setup_id)
        if build.state != SetupState.QUEUED or build.cancel_requested_at is not None:
            return None
        build.state = SetupState.RUNNING
        build.worker = worker
        build.lease_token = uuid.uuid4()
        build.lease_expires_at = now + LEASE_TTL
        build.started_at = now
        build.save(
            update_fields=["state", "worker", "lease_token", "lease_expires_at", "started_at"]
        )
        live.setup_changed(build.pk)
        return build, payload(build)


def payload(build: SetupBuild) -> dict[str, Any]:
    """What the worker reads (``spec/…/setup-input/v1``)."""
    from django.conf import settings

    assert build.lease_token is not None and build.lease_expires_at is not None  # noqa: S101
    plugin = build.environment.partition(".")[0]
    proxy = {}
    if build.spec.get("network") and settings.IKI_EGRESS_PROXY_URL:
        from urllib.parse import urlsplit

        netloc = urlsplit(settings.IKI_EGRESS_PROXY_URL).netloc
        proxy = {"proxy": f"http://{build.pk}:{build.lease_token}@{netloc}"}
    return {
        "apiVersion": "iki/v1",
        "kind": "SetupInput",
        "setup": {
            "id": str(build.pk),
            "hrid": build.hrid,
            "version": build.version,
            "environment": build.environment,
            "plugin": {
                "name": plugin,
                "version": build.plugin_version,
                "sha": build.plugin_sha or None,
            },
        },
        "job": {
            "id": str(build.pk),
            "lease_token": str(build.lease_token),
            "lease_expires_at": build.lease_expires_at.isoformat(),
            "heartbeat_every": int(HEARTBEAT_EVERY.total_seconds()),
            "timeout": build.timeout,
            **proxy,
        },
        "script": build.spec["script"],
        "volumes": build.spec["volumes"],
        "settings": _settings(plugin),
    }


def _settings(plugin: str) -> dict[str, Any]:
    """The deployment's values for the plugin's settings, as its runs get them."""
    from django.core.exceptions import ImproperlyConfigured

    if not registry.plugins()[plugin].settings:
        return {}
    try:
        values = registry.settings(plugin)
    except ImproperlyConfigured:
        return {}
    return {name: value for name, value in values.items() if value is not None}


def _holds(worker: Worker, build_id: uuid.UUID, token: uuid.UUID) -> SetupBuild:
    build = SetupBuild.objects.select_for_update().filter(pk=build_id, worker=worker).first()
    if (
        build is None
        or build.state != SetupState.RUNNING
        or build.lease_token != token
        or build.lease_expires_at is None
        or build.lease_expires_at <= timezone.now()
    ):
        raise Stale
    return build


def heartbeat(worker: Worker, build_id: uuid.UUID, token: uuid.UUID) -> tuple[datetime, bool]:
    """Renews the lease. Returns the new expiry, and whether the build was cancelled."""
    with transaction.atomic():
        build = _holds(worker, build_id, token)
        build.lease_expires_at = timezone.now() + LEASE_TTL
        build.save(update_fields=["lease_expires_at"])
        return build.lease_expires_at, build.cancel_requested_at is not None


def progress(
    worker: Worker, build_id: uuid.UUID, token: uuid.UUID, fraction: float, message: str
) -> None:
    with transaction.atomic():
        build = _holds(worker, build_id, token)
        build.progress = max(0.0, min(1.0, fraction))
        build.progress_message = message[:500]
        build.save(update_fields=["progress", "progress_message"])
        live.setup_changed(build.pk)


def append_log(
    worker: Worker, build_id: uuid.UUID, token: uuid.UUID, offset: int, text: str
) -> int:
    """Adds a chunk at `offset`, as a run's job's log does."""
    size = len(text.encode())
    with transaction.atomic():
        build = _holds(worker, build_id, token)
        end = build.log_size
        if offset < end and offset + size <= end:
            return end
        if offset != end:
            raise LogGap(end)
        build.log_size = end + size
        build.save(update_fields=["log_size"])
        live.setup_changed(build.pk)
        if end >= MAX_LOG_BYTES:
            return build.log_size
        if end + size > MAX_LOG_BYTES:
            text = text.encode()[: MAX_LOG_BYTES - end].decode(errors="ignore")
            text += (
                f"\n[iki: the log is over {MAX_LOG_BYTES // 1024**2} MiB; the rest isn't kept]\n"
            )
        SetupLog.objects.create(build=build, offset=offset, text=text.replace("\x00", "�"))
        return build.log_size


#: How a build's script ended, as its worker reports it → the build's state.
OUTCOMES = {
    "failed": SetupState.FAILED,
    "timed_out": SetupState.TIMED_OUT,
    "interrupted": SetupState.FAILED,
    "cancelled": SetupState.CANCELLED,
}


def finish(
    worker: Worker,
    build_id: uuid.UUID,
    token: uuid.UUID,
    outcome: str,
    message: str = "",
    data_version: str = "",
) -> SetupBuild:
    """The script ended. When it succeeded, its volumes are snapshotted next; otherwise the
    build ends. A cancelled build ends cancelled, whatever happened."""
    with transaction.atomic():
        build = _holds(worker, build_id, token)
        now = timezone.now()
        if build.cancel_requested_at is not None:
            _end(build, SetupState.CANCELLED, None, now)
        elif outcome == "succeeded":
            build.state = SetupState.SNAPSHOTTING
            build.data_version = data_version.strip()[:200]
            build.lease_token = build.lease_expires_at = None
            build.save(update_fields=["state", "data_version", "lease_token", "lease_expires_at"])
            live.setup_changed(build.pk)
            # Its machine is done: the provisioner snapshots its disks and ends it.
            from iki_server.runs import queue

            queue.notify("container")
        else:
            _end(build, OUTCOMES[outcome], {"code": outcome, "message": message[:2000]}, now)
        return build


def recover_expired() -> int:
    """Ends builds whose worker stopped sending heartbeats, and those running well past
    their timeout. The task runner calls it with runs' jobs'."""
    now = timezone.now()
    ended = 0
    with transaction.atomic():
        for build in SetupBuild.objects.select_for_update(skip_locked=True).filter(
            Q(lease_expires_at__lt=now)
            | Q(started_at__lt=now - TIMEOUT_GRACE, lease_expires_at__isnull=False),
            state=SetupState.RUNNING,
        )[:100]:
            assert build.started_at is not None  # noqa: S101 - a running build has started
            if build.lease_expires_at is not None and build.lease_expires_at < now:
                _end(
                    build,
                    SetupState.FAILED,
                    {"code": "worker_lost", "message": "Its worker stopped sending heartbeats."},
                    now,
                )
                ended += 1
            elif build.started_at + timedelta(seconds=build.timeout) + TIMEOUT_GRACE < now:
                _end(
                    build,
                    SetupState.TIMED_OUT,
                    {
                        "code": "timed_out",
                        "message": f"It ran past its {build.timeout}s timeout, and its worker "
                        "didn't stop it.",
                    },
                    now,
                )
                ended += 1
    return ended
