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
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone

from iki_server.runs import catalog, launches, setups
from iki_server.runs.kubernetes import (
    Cluster,
    KubernetesError,
    RunPods,
    claim_manifest,
    job_manifest,
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

__all__ = ["SETUP_LABEL", "tick"]

logger = structlog.get_logger("iki.provisioner")

#: Disks and snapshots for a setup build carry this label, with its ID.
SETUP_LABEL = "iki.ikisci.org/setup"
#: And the volume's name.
VOLUME_LABEL = "iki.ikisci.org/volume"
#: A machine for a build that couldn't start this many times ends the build.
START_TRIES = 3
#: How long a snapshot may take to become ready before the build fails.
SNAPSHOT_WITHIN = timedelta(hours=6)


def _claim(build: SetupBuild, volume: str) -> str:
    names = sorted(build.spec["volumes"])
    return f"iki-setup-{build.pk.hex}-{names.index(volume)}"


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
                moved += _snapshot(cluster, build, now)
        except (KubernetesError, OSError) as error:
            logger.warning("setup_tick_failed", build=build.hrid, error=str(error))
    for namespace in namespaces:
        try:
            _tidy(cluster, namespace)
        except (KubernetesError, OSError) as error:
            logger.warning("setup_tidy_failed", namespace=namespace, error=str(error))
    return moved


# --- Starting a build's machine -------------------------------------------------------------


def _start(cluster: Cluster, pods: RunPods, build: SetupBuild, now: datetime) -> int:
    tries = list(Launch.objects.filter(setup=build).order_by("-created_at"))
    if any(each.state != LaunchState.ENDED for each in tries):
        return 0  # its machine is starting, or its worker is about to lease it
    # One whose worker didn't lease it counts too: it'd go the same way again.
    failed = tries
    if len(failed) >= START_TRIES:
        with transaction.atomic():
            locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
            if locked.state == SetupState.QUEUED:
                setups.end(
                    locked,
                    SetupState.FAILED,
                    {
                        "code": "machine_unavailable",
                        "message": f"No machine could start for it, {len(failed)} times: "
                        f"{failed[0].message or failed[0].ended_reason}",
                    },
                )
        return 1
    template = catalog.compute().templates.get(build.template)
    if template is None:
        with transaction.atomic():
            locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
            setups.end(
                locked,
                SetupState.FAILED,
                {
                    "code": "template_gone",
                    "message": f"Its compute template, {build.template}, is gone.",
                },
            )
        return 1
    claims: dict[str, tuple[str, str]] = {}
    have = {
        c["metadata"]["name"] for c in cluster.list(build.place, "pvc", f"{SETUP_LABEL}={build.pk}")
    }
    for volume, declared in sorted(build.spec["volumes"].items()):
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
        claims[volume] = (name, declared["mount"])
    launch, token = launches.create(
        template=template.name,
        provisioner=template.provisioner,
        environment=build.environment,
        plugin_sha=build.plugin_sha,
        image=build.image,
        namespace=build.place,
        once=True,
        setup=build,
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


def _snapshot(cluster: Cluster, build: SetupBuild, now: datetime) -> int:
    if Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).exists():
        return 0  # its pod still has the disks mounted
    template = catalog.compute().templates.get(build.template)
    snapshots = {
        s["metadata"]["name"]: s
        for s in cluster.list(build.place, "snapshot", f"{SETUP_LABEL}={build.pk}")
    }
    ready = 0
    failure = ""
    for volume, declared in sorted(build.spec["volumes"].items()):
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
    stuck = build.finished_at is None and (now - (build.started_at or build.created_at)) > (
        timedelta(seconds=build.timeout) + SNAPSHOT_WITHIN
    )
    if failure or stuck:
        message = failure or "Its snapshots didn't become ready in time."
        with transaction.atomic():
            locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
            if locked.state == SetupState.SNAPSHOTTING:
                ManagedVolume.objects.filter(build=locked).exclude(state=VolumeState.READY).update(
                    state=VolumeState.FAILED, message=message[:500]
                )
                setups.end(
                    locked, SetupState.FAILED, {"code": "snapshot_failed", "message": message}
                )
        return 1
    if ready < len(build.spec["volumes"]):
        return 0
    with transaction.atomic():
        locked = SetupBuild.objects.select_for_update().get(pk=build.pk)
        if locked.state != SetupState.SNAPSHOTTING:
            return 0
        setups.end(locked, SetupState.SUCCEEDED)
    logger.info("setup_succeeded", build=build.hrid, version=build.version)
    return 1


# --- Tidying up -------------------------------------------------------------------------------


def _in_use(build_id: str) -> bool:
    """Whether a live machine has a copy of the build's volumes."""
    live = Launch.objects.exclude(state=LaunchState.ENDED).exclude(volumes={})
    return any(
        (volume or {}).get("build") == build_id
        for volumes in live.values_list("volumes", flat=True)
        for volume in volumes.values()
    )


def _tidy(cluster: Cluster, namespace: str) -> None:
    """Deletes disks once their build is done with them, and snapshots once nothing will
    copy them again."""
    builds: dict[str, Any] = {}

    def build_of(item: dict[str, Any]) -> SetupBuild | None:
        key = ((item.get("metadata") or {}).get("labels") or {}).get(SETUP_LABEL, "")
        if key not in builds:
            try:
                builds[key] = SetupBuild.objects.filter(pk=key).first()
            except ValueError:
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
        keep = build.state == SetupState.SUCCEEDED and (
            build.retired_at is None or _in_use(str(build.pk))
        )
        if keep:
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
