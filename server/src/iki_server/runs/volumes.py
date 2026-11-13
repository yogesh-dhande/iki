"""Environment setup builds on Kubernetes, and the snapshots they leave (ADR 0075).

Each tick, for each build in a namespace the provisioner manages:

- **Queued:** a disk (PersistentVolumeClaim) for each volume the setup fills, with the
  StorageClass the template names, then a pod with them mounted writable, started as any
  launch is, whose worker leases the build.
- **Snapshotting:** once the script succeeded and the pod has gone (so the disks are no
  longer mounted), a VolumeSnapshot of each disk. Once every snapshot is ready to use, the
  build has succeeded: a new version of each volume, which machines started for the
  environment's runs get a copy of. The disks are then deleted.
- **Ended otherwise:** its disks, and any snapshots, are deleted.
- **Retired:** its snapshots are deleted once no live machine has a copy of them.

Disks and snapshots carry the label ``iki.ikisci.org/setup`` with their build's ID; any
whose build is gone are deleted.

A volume on a shared file system (ADR 0077) has no disk or snapshot: the build's pod
mounts only the version's own directory there, and the version is ready once the
script succeeds and the pod has gone. A Job removes the directory once it isn't kept,
and carries the same label.

A volume in a bucket (ADR 0078), mounted with Mountpoint for Amazon S3, can't be
written in place (no renames, nor writes but in order), so the build fills a disk, as
for a snapshot. Once the pod has gone, a Job copies it into the version's own directory
(a prefix) in the bucket, and the version is ready once it has. It's then removed as
one on a shared file system is.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from iki_server.runs import catalog, launches, setups
from iki_server.runs.kubernetes import (
    Cluster,
    KubernetesError,
    RunPods,
    claim_manifest,
    job_manifest,
    publish_manifest,
    removal_manifest,
    secret_manifest,
    snapshot_manifest,
)
from iki_server.runs.models import (
    Launch,
    LaunchState,
    ManagedVolume,
    SetupBuild,
    SetupState,
    VolumeState,
)
from iki_spec.compute import Template

__all__ = [
    "SETUP_LABEL",
    "fail",
    "in_use",
    "kept",
    "ready_to_start",
    "record_shared",
    "removed",
    "shared_ready",
    "stuck",
    "succeed",
    "template_of",
    "tick",
    "to_remove",
]

logger = structlog.get_logger("iki.provisioner")

#: Disks and snapshots for a setup build carry this label, with its ID.
SETUP_LABEL = "iki.ikisci.org/setup"
#: And the volume's name.
VOLUME_LABEL = "iki.ikisci.org/volume"
#: A machine for a build that couldn't start this many times ends the build.
START_TRIES = 3
#: After a platform had no machine to give, the wait before trying again, doubling each
#: time, up to the most. Those tries don't count towards :data:`START_TRIES`.
BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=10)
#: A build in these states has ended.
FINISHED = (
    SetupState.SUCCEEDED,
    SetupState.FAILED,
    SetupState.TIMED_OUT,
    SetupState.CANCELLED,
)
#: How long a snapshot may take to become ready before the build fails.
SNAPSHOT_WITHIN = timedelta(hours=6)


def _claim(build: SetupBuild, volume: str) -> str:
    names = sorted(build.spec["volumes"])
    return f"iki-setup-{build.pk.hex}-{names.index(volume)}"


def _removal(build: SetupBuild, volume: str) -> str:
    names = sorted(build.spec["volumes"])
    return f"iki-remove-{build.pk.hex}-{names.index(volume)}"


def _publish(build: SetupBuild, volume: str) -> str:
    names = sorted(build.spec["volumes"])
    return f"iki-publish-{build.pk.hex}-{names.index(volume)}"


def _ended(job: dict[str, Any]) -> str:
    """How a Job ended: ``succeeded``, ``failed``, or empty while it hasn't."""
    status = job.get("status") or {}
    conditions = {c.get("type"): c.get("status") == "True" for c in status.get("conditions") or []}
    if conditions.get("Complete") or status.get("succeeded"):
        return "succeeded"
    return "failed" if conditions.get("Failed") else ""


def _labels(build: SetupBuild, volume: str) -> dict[str, str]:
    return {SETUP_LABEL: str(build.pk), VOLUME_LABEL: volume}


def tick(cluster: Cluster, pods: RunPods, namespaces: list[str], now: datetime) -> int:
    """One round for every build whose disks or snapshots the provisioner manages.
    Returns how many builds it moved on."""
    moved = 0
    unfinished = SetupBuild.objects.filter(state__in=[SetupState.QUEUED, SetupState.SNAPSHOTTING])
    for build in unfinished.filter(place__in=namespaces).order_by("created_at"):
        try:
            if build.state == SetupState.QUEUED:
                moved += _start(cluster, pods, build, now)
            else:
                moved += _snapshot(cluster, pods, build, now)
        except (KubernetesError, OSError) as error:
            logger.warning("setup_tick_failed", build=build.hrid, error=str(error))
    for namespace in namespaces:
        try:
            _tidy(cluster, pods, namespace)
        except (KubernetesError, OSError) as error:
            logger.warning("setup_tidy_failed", namespace=namespace, error=str(error))
    return moved


# --- Starting a build's machine -------------------------------------------------------------


def ready_to_start(build: SetupBuild, now: datetime) -> bool:
    """Whether to start a machine for the build now: none is starting, and fewer than
    :data:`START_TRIES` failed to (it fails when they have). A platform with no machine to
    give is waited for, longer each time."""
    tries = list(Launch.objects.filter(setup=build).order_by("-created_at"))
    if any(each.state != LaunchState.ENDED for each in tries):
        return False  # its machine is starting, or its worker is about to lease it
    # One whose worker didn't lease it counts too: it'd go the same way again.
    failed = [each for each in tries if each.ended_reason != "no_capacity"]
    if len(failed) >= START_TRIES:
        fail(
            build,
            SetupState.QUEUED,
            "machine_unavailable",
            f"No machine could start for it, {len(failed)} times: "
            f"{failed[0].message or failed[0].ended_reason}",
        )
        return False
    waited = [each for each in tries if each.ended_reason == "no_capacity"]
    if waited and tries[0].ended_reason == "no_capacity" and tries[0].ended_at is not None:
        wait = min(BACKOFF * 2 ** (len(waited) - 1), MAX_BACKOFF)
        if tries[0].ended_at + wait > now:
            return False
    return True


def template_of(build: SetupBuild) -> Template | None:
    """The build's compute template; when it's gone, the build fails."""
    template = catalog.compute().templates.get(build.template)
    if template is None:
        fail(
            build,
            build.state,
            "template_gone",
            f"Its compute template, {build.template}, is gone.",
        )
    return template


def fail(build: SetupBuild, was: str, code: str, message: str) -> None:
    """Ends the build failed, if it's still `was`."""
    with transaction.atomic():
        locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
        if locked.state != was:
            return
        if was == SetupState.SNAPSHOTTING:
            ManagedVolume.objects.filter(build=locked).exclude(state=VolumeState.READY).update(
                state=VolumeState.FAILED, message=message[:500]
            )
        setups.end(locked, SetupState.FAILED, {"code": code, "message": message})


def succeed(build: SetupBuild) -> int:
    """Ends the build succeeded, once every volume's snapshot is ready."""
    with transaction.atomic():
        locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
        if locked.state != SetupState.SNAPSHOTTING:
            return 0
        setups.end(locked, SetupState.SUCCEEDED)
    logger.info("setup_succeeded", build=build.hrid, version=build.version)
    return 1


def stuck(build: SetupBuild, now: datetime, within: timedelta) -> bool:
    """Whether its snapshots have taken longer than `within` past its script's timeout."""
    began = build.started_at or build.created_at
    return now - began > timedelta(seconds=build.timeout) + within


def kept(build: SetupBuild) -> bool:
    """Whether a finished build's snapshots are kept: it succeeded, and it isn't retired or
    a live machine still has a copy."""
    return build.state == SetupState.SUCCEEDED and (
        build.retired_at is None or in_use(str(build.pk))
    )


def record_shared(build: SetupBuild, volume: str) -> None:
    """Records the build's version of a volume on a shared file system before anything is
    written there, so it's removed if the build fails."""
    declared = build.spec["volumes"][volume]
    ManagedVolume.objects.get_or_create(
        build=build,
        name=volume,
        defaults={
            "environment": build.environment,
            "place": build.place,
            "handle": declared["directory"],
            "size": int(declared["size"]),
        },
    )


def shared_ready(build: SetupBuild, volume: str, now: datetime) -> None:
    """What the script wrote is the version: nothing to snapshot."""
    ManagedVolume.objects.filter(build=build, name=volume).exclude(state=VolumeState.READY).update(
        state=VolumeState.READY, ready_at=now
    )


def to_remove(place: str) -> dict[SetupBuild, list[ManagedVolume]]:
    """The versions on shared file systems in `place` to remove now, by build: no longer
    kept, and no machine of their build still writing."""
    found: dict[SetupBuild, list[ManagedVolume]] = {}
    going = ManagedVolume.objects.filter(
        place=place, deleted_at__isnull=True, build__state__in=FINISHED
    ).select_related("build")
    for record in going.order_by("created_at"):
        build = record.build
        if (
            record.name not in build.spec["volumes"]
            or not setups.shared(build, record.name)
            or kept(build)
            # A build's machine may still be writing, if the build ended without it.
            or Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).exists()
        ):
            continue
        found.setdefault(build, []).append(record)
    return found


def removed(build: SetupBuild, records: list[ManagedVolume]) -> None:
    """Deletes versions once their directories are gone."""
    ManagedVolume.objects.filter(pk__in=[r.pk for r in records]).update(
        state=VolumeState.DELETED if build.state == SetupState.SUCCEEDED else VolumeState.FAILED,
        deleted_at=timezone.now(),
        message="",
    )


def _start(cluster: Cluster, pods: RunPods, build: SetupBuild, now: datetime) -> int:
    if not ready_to_start(build, now):
        build.refresh_from_db(fields=["state"])
        return int(build.state != SetupState.QUEUED)
    template = template_of(build)
    if template is None:
        return 1
    claims: dict[str, dict[str, str]] = {}
    recorded: dict[str, dict[str, Any]] = {}
    have = {
        c["metadata"]["name"] for c in cluster.list(build.place, "pvc", f"{SETUP_LABEL}={build.pk}")
    }
    for volume, declared in sorted(build.spec["volumes"].items()):
        if setups.shared(build, volume) and not setups.staged(build, volume):
            # Its version's own directory on the file system, made before it starts.
            claims[volume] = {
                "claim": declared["claim"],
                "mount": declared["mount"],
                "root": declared["root"],
                "directory": declared["directory"],
            }
            recorded[volume] = {"claim": declared["claim"], "directory": declared["directory"]}
            record_shared(build, volume)
            continue
        name = _claim(build, volume)
        if name not in have:
            cluster.create(
                build.place,
                "pvc",
                claim_manifest(
                    name=name,
                    template=template,
                    volume=volume,
                    size=int(declared["size"]),
                    labels={**pods.labels, **_labels(build, volume)},
                ),
            )
        claims[volume] = {"claim": name, "mount": declared["mount"]}
        recorded[volume] = {"disk": name}
        if setups.staged(build, volume):
            # Recorded before anything's copied into the bucket, so a failed build's
            # partial copy is removed.
            record_shared(build, volume)
    launch, token = launches.create(
        template=template.name,
        provisioner=template.provisioner,
        environment=build.environment,
        plugin_sha=build.plugin_sha,
        image=build.image,
        namespace=build.place,
        once=True,
        setup=build,
        # Its worker leases the build once every disk it fills is known.
        volumes=recorded,
    )
    job, secret = job_manifest(
        name=launch.name,
        launch_id=str(launch.pk),
        template=template,
        environment=build.environment,
        image=build.image,
        pods=pods,
        once=True,
        claims=claims,
    )
    try:
        cluster.create_job(
            build.place, job, secret_manifest(secret, str(launch.pk), token, pods.labels)
        )
    except (KubernetesError, OSError) as error:
        launches.end(launch, "failed", f"Kubernetes didn't start it: {error}")
        raise
    logger.info("setup_launched", build=build.hrid, launch=str(launch.pk))
    return 1


# --- Snapshotting what it filled ------------------------------------------------------------


def _snapshot(cluster: Cluster, pods: RunPods, build: SetupBuild, now: datetime) -> int:
    if Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).exists():
        return 0  # its pod still has the disks mounted
    template = catalog.compute().templates.get(build.template)
    snapshots = {
        s["metadata"]["name"]: s
        for s in cluster.list(build.place, "snapshot", f"{SETUP_LABEL}={build.pk}")
    }
    ready = 0
    failure = ""
    publishing = False
    for volume, declared in sorted(build.spec["volumes"].items()):
        if setups.staged(build, volume):
            published, failure_of = _published(cluster, pods, build, volume, now)
            ready += published
            publishing = publishing or not (published or failure_of)
            failure = failure or failure_of
            continue
        if setups.shared(build, volume):
            shared_ready(build, volume, now)
            ready += 1
            continue
        name = _claim(build, volume)
        record, _ = ManagedVolume.objects.get_or_create(
            build=build,
            name=volume,
            defaults={
                "environment": build.environment,
                "place": build.place,
                "handle": name,
                "size": int(declared["size"]),
            },
        )
        found = snapshots.get(name)
        if found is None:
            if template is None:
                failure = f"Its compute template, {build.template}, is gone."
                continue
            cluster.create(
                build.place,
                "snapshot",
                snapshot_manifest(
                    name=name,
                    claim=name,
                    template=template,
                    volume=volume,
                    labels=_labels(build, volume),
                ),
            )
            continue
        status = found.get("status") or {}
        if status.get("readyToUse") is True:
            if record.state != VolumeState.READY:
                record.state = VolumeState.READY
                record.ready_at = now
                record.save(update_fields=["state", "ready_at"])
            ready += 1
        elif (status.get("error") or {}).get("message"):
            failure = f"Its snapshot of {volume} failed: {status['error']['message']}"
    # A copy into a bucket is given its own time, which ends it as failed when it's up.
    if (
        not failure
        and not publishing
        and ready < len(build.spec["volumes"])
        and build.finished_at is None
        and stuck(build, now, SNAPSHOT_WITHIN)
    ):
        failure = "Its snapshots didn't become ready in time."
    if failure:
        fail(build, SetupState.SNAPSHOTTING, "snapshot_failed", failure)
        return 1
    if ready < len(build.spec["volumes"]):
        return 0
    return succeed(build)


def _published(
    cluster: Cluster, pods: RunPods, build: SetupBuild, volume: str, now: datetime
) -> tuple[int, str]:
    """Copies what the build wrote to its disk for the volume into the version's directory
    in the bucket, with a Job. Returns 1 once it has, or why it couldn't."""
    if ManagedVolume.objects.filter(build=build, name=volume, state=VolumeState.READY).exists():
        return 1, ""
    name = _publish(build, volume)
    found = cluster.list(build.place, "job", f"{SETUP_LABEL}={build.pk}")
    job = next((j for j in found if j["metadata"]["name"] == name), None)
    if job is None:
        declared = build.spec["volumes"][volume]
        cluster.create(
            build.place,
            "job",
            publish_manifest(
                name=name,
                staging=_claim(build, volume),
                claim=declared["claim"],
                root=declared["root"],
                directory=declared["directory"],
                pods=pods,
                labels=_labels(build, volume),
            ),
        )
        logger.info("setup_publishing", build=build.hrid, volume=volume)
        return 0, ""
    ended = _ended(job)
    if ended == "succeeded":
        shared_ready(build, volume, now)
        cluster.delete(build.place, "job", name)
        logger.info("setup_published", build=build.hrid, volume=volume)
        return 1, ""
    if ended == "failed":
        # The Job is kept until Kubernetes deletes it, so its pod's log can be read.
        return 0, (
            f"Iki couldn't copy {volume} into its bucket. The log of the Job {name}, in "
            f"{build.place}, says why for a few minutes."
        )
    return 0, ""


# --- Tidying up -------------------------------------------------------------------------------


def in_use(build_id: str) -> bool:
    """Whether a live machine has a copy of the build's volumes."""
    live = Launch.objects.exclude(state=LaunchState.ENDED).exclude(volumes={})
    return any(
        (volume or {}).get("build") == build_id
        for volumes in live.values_list("volumes", flat=True)
        for volume in volumes.values()
    )


def _tidy(cluster: Cluster, pods: RunPods, namespace: str) -> None:
    """Deletes disks once their build is done with them, snapshots once nothing will copy
    them again, and versions' directories on shared file systems once nothing will mount
    them again."""
    builds: dict[str, Any] = {}

    def build_of(item: dict[str, Any]) -> SetupBuild | None:
        key = ((item.get("metadata") or {}).get("labels") or {}).get(SETUP_LABEL, "")
        if key not in builds:
            try:
                builds[key] = SetupBuild.objects.filter(pk=key).first()
            except (ValueError, ValidationError):  # not a build's ID
                builds[key] = None
        found: SetupBuild | None = builds[key]
        return found

    for claim in cluster.list(namespace, "pvc", SETUP_LABEL):
        build = build_of(claim)
        if build is None or build.state not in (
            SetupState.QUEUED,
            SetupState.RUNNING,
            SetupState.SNAPSHOTTING,
        ):
            cluster.delete(namespace, "pvc", claim["metadata"]["name"])
    for snapshot in cluster.list(namespace, "snapshot", SETUP_LABEL):
        build = build_of(snapshot)
        name = snapshot["metadata"]["name"]
        if build is None:
            cluster.delete(namespace, "snapshot", name)
            continue
        if build.state in (SetupState.SNAPSHOTTING, SetupState.RUNNING, SetupState.QUEUED):
            continue
        if kept(build):
            continue
        cluster.delete(namespace, "snapshot", name)
        ManagedVolume.objects.filter(build=build, handle=name).exclude(
            state=VolumeState.DELETED
        ).update(
            state=VolumeState.DELETED
            if build.state == SetupState.SUCCEEDED
            else VolumeState.FAILED,
            deleted_at=timezone.now(),
        )
    _remove_shared(cluster, pods, namespace, build_of)


def _remove_shared(
    cluster: Cluster,
    pods: RunPods,
    namespace: str,
    build_of: Callable[[dict[str, Any]], SetupBuild | None],
) -> None:
    """Removes each version's directory on a shared file system once it isn't kept, with a
    Job of the worker's image mounting the file system's directory for Iki. The version is
    deleted once the Job has done it."""
    jobs = {j["metadata"]["name"]: j for j in cluster.list(namespace, "job", SETUP_LABEL)}
    wanted: set[str] = set()
    for build, records in to_remove(namespace).items():
        for record in records:
            copying = jobs.get(_publish(build, record.name))
            if copying is not None and not _ended(copying):
                continue  # still writing there: removed once it's ended
            name = _removal(build, record.name)
            wanted.add(name)
            found = jobs.get(name)
            if found is None:
                declared = build.spec["volumes"][record.name]
                cluster.create(
                    namespace,
                    "job",
                    removal_manifest(
                        name=name,
                        claim=declared["claim"],
                        root=declared["root"],
                        directories=[declared["directory"]],
                        pods=pods,
                        labels=_labels(build, record.name),
                    ),
                )
                logger.info("setup_directory_removing", build=build.hrid, volume=record.name)
                continue
            ended = _ended(found)
            if ended == "succeeded":
                removed(build, [record])
                cluster.delete(namespace, "job", name)
                logger.info("setup_directory_removed", build=build.hrid, volume=record.name)
                continue
            failed = ended == "failed"
            # A failed Job is kept until Kubernetes deletes it (KEEP_FINISHED after it
            # ends), so its pod's log can be read; then it's started again, from the start.
            # Removing is safe to repeat, and one that keeps failing is tried only that often.
            if failed and not record.message:
                ManagedVolume.objects.filter(pk=record.pk).update(
                    message="Iki couldn't remove its directory, and tries again shortly."
                )
                logger.warning(
                    "setup_directory_removal_failed", build=build.hrid, volume=record.name
                )
    for name, job in jobs.items():
        if name not in wanted and build_of(job) is None:
            cluster.delete(namespace, "job", name)
