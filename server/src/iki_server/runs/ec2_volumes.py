"""Environment setup builds on the vm tier, and the EBS snapshots they leave (ADR 0076).

Each round, for each build in a region the deployment's EC2 templates use:

- **Queued:** an on-demand instance of the build's template, as a run's machine is
  started, with an empty, encrypted disk for each volume the setup fills, kept when the
  instance ends. Once EC2 lists which disk is attached as which device, each is tagged
  with its volume's name and recorded on the launch; only then may its worker lease the
  build, so every disk it writes can be found again.
- **Snapshotting:** once the instance has ended and its disks are detached (so nothing is
  still writing), an EBS snapshot of each. When the template asks for fast snapshot
  restore, each completed snapshot is then made fast to restore in the template's zones.
  Once every snapshot is ready, the build has succeeded and its disks are deleted.
- **Ended otherwise:** its disks, and any snapshots, are deleted.
- **Succeeded:** only the version machines get now is kept fast to restore, which costs
  by the hour: an older one stops being, and one that's current again (its successor
  retired) is made fast again. A retired version's snapshots are deleted once no live
  machine has a copy.

Disks and snapshots carry the tags ``iki.ikisci.org/setup`` (their build's ID) and
``iki.ikisci.org/volume``, besides the deployment's own; any whose build is gone are
deleted.

A volume on a shared file system (ADR 0077) has no disk or snapshot: the build's machine
mounts the file system and gives its container only the version's own directory, and the
version is ready once the script succeeds and the machine has ended. Once a version isn't
kept, a small on-demand machine of the build's template mounts the file system, removes
it, says so on its console, and shuts down; one that didn't is started again later.

A volume in an S3 bucket (ADR 0078) gets a disk, as for a snapshot, that the build's
machine copies into the version's own directory in the bucket once its worker has said
the build succeeded, and says so on its console (``IKI-PUBLISHED <volume>``) before it
shuts down. The machine is left to finish though its launch has ended; the version is
ready once its console says so, and the build fails if it says it couldn't, or takes
longer than :data:`PUBLISH_WITHIN`. The disk is then deleted, and the version is removed
as one on a shared file system is.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from django.utils import timezone

from iki_server.runs import catalog, launches, queue, setups, volumes
from iki_server.runs.ec2 import (
    DEVICES,
    ENVIRONMENT_TAG,
    LAUNCH_TAG,
    OWNER_TAG,
    PUBLISHED,
    REMOVAL_TAG,
    REMOVED,
    SETUP_TAG,
    VOLUME_TAG,
    Disk,
    Ec2Error,
    cleanup_request,
    disks,
    host_trouble,
    is_capacity,
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

if TYPE_CHECKING:
    from iki_server.runs.ec2_fleet import Ec2Fleet

__all__ = ["tick"]

logger = structlog.get_logger("iki.provisioner")

#: How long past its script's timeout a build's snapshots (and making them fast to
#: restore) may take before it fails: EBS copies a terabyte-sized disk's first snapshot in
#: hours, and makes it fast to restore at about a terabyte an hour.
SNAPSHOT_WITHIN = timedelta(hours=24)
#: EC2 lists a new snapshot a little after it says it took it.
LIST_LAG = timedelta(minutes=2)
#: How often to look for disks and snapshots nothing needs, in each region.
TIDY_EVERY = timedelta(minutes=5)
UNFINISHED = (SetupState.QUEUED, SetupState.RUNNING, SetupState.SNAPSHOTTING)
#: Fast snapshot restore states that mean it is, or will be, fast to restore in a zone.
FAST = ("enabling", "optimizing", "enabled")
#: A machine removing versions from a shared file system that hasn't said it did is
#: started again this long after the last one started, and stopped after the longest.
CLEANUP_AGAIN = timedelta(minutes=15)
CLEANUP_WITHIN = timedelta(hours=6)
#: How long a setup build's machine may take to copy its disks into a bucket, once its
#: worker has gone, before the build fails; and how long its console may take to say so
#: once it has ended (EC2 keeps it for an hour).
PUBLISH_WITHIN = timedelta(hours=24)
CONSOLE_WITHIN = timedelta(minutes=30)
#: Instance states that mean it has ended, or is ending.
ENDED = ("shutting-down", "terminated", "stopping", "stopped")


def _tag(item: dict[str, Any], key: str) -> str | None:
    for tag in item.get("Tags") or []:
        if tag.get("Key") == key:
            return str(tag.get("Value"))
    return None


class Region:
    """What EC2 says is in one region, listed at most once a round."""

    def __init__(self, fleet: Ec2Fleet, name: str, instances: list[dict[str, Any]]) -> None:
        self.fleet = fleet
        self.name = name
        self.instances = instances
        self._volumes: dict[str, dict[str, Any]] | None = None
        self._snapshots: list[dict[str, Any]] | None = None

    @property
    def ec2(self) -> Any:  # noqa: ANN401 - the fleet's Ec2
        return self.fleet.ec2

    def volumes(self) -> dict[str, dict[str, Any]]:
        """The deployment's setup builds' disks here, by their ID."""
        if self._volumes is None:
            listed = self.ec2.volumes(self.name, self.fleet.machines.owner)
            self._volumes = {str(v["VolumeId"]): v for v in listed}
        return self._volumes

    def snapshots(self) -> list[dict[str, Any]]:
        if self._snapshots is None:
            self._snapshots = list(self.ec2.snapshots(self.name, self.fleet.machines.owner))
        return self._snapshots

    def snapshot_of(self, build: SetupBuild, volume: str) -> dict[str, Any] | None:
        """The build's snapshot of `volume`: the one Iki recorded, or failing that the first
        it took (in case it took one it couldn't record)."""
        record = ManagedVolume.objects.filter(build=build, name=volume).first()
        mine = [
            s
            for s in self.snapshots()
            if _tag(s, SETUP_TAG) == str(build.pk) and _tag(s, VOLUME_TAG) == volume
        ]
        if record is not None:
            return next((s for s in mine if s["SnapshotId"] == record.handle), None)
        return min(mine, key=lambda s: str(s.get("StartTime", "")), default=None)


def tick(fleet: Ec2Fleet, listed: dict[str, list[dict[str, Any]]], now: datetime) -> int:
    """One round for every build on the vm tier, in the regions whose instances were
    `listed`. Returns how many builds it moved on."""
    moved = 0
    for name, instances in sorted(listed.items()):
        region = Region(fleet, name, instances)
        builds = SetupBuild.objects.filter(place=name, state__in=UNFINISHED).order_by("created_at")
        for build in builds:
            try:
                if build.state == SetupState.QUEUED:
                    _record_disks(region, build)
                    moved += _start(fleet, build, name, now)
                elif build.state == SetupState.SNAPSHOTTING:
                    moved += _snapshot(region, build, now)
            except Ec2Error as error:
                logger.warning("setup_tick_failed", build=build.hrid, error=str(error))
        last = fleet.tidied.get(name)
        if last is None or now - last >= TIDY_EVERY:
            try:
                _tidy(region, now)
                fleet.tidied[name] = now
            except Ec2Error as error:
                logger.warning("setup_tidy_failed", region=name, error=str(error))
    return moved


# --- Starting a build's machine -------------------------------------------------------------


def _start(fleet: Ec2Fleet, build: SetupBuild, region: str, now: datetime) -> int:
    if not volumes.ready_to_start(build, now):
        build.refresh_from_db(fields=["state"])
        return int(build.state != SetupState.QUEUED)
    template = volumes.template_of(build)
    if template is None:
        return 1
    launch, token = launches.create(
        template=template.name,
        provisioner=template.provisioner,
        environment=build.environment,
        plugin_sha=build.plugin_sha,
        image=build.image,
        namespace=region,
        once=True,
        setup=build,
    )
    # On demand: a spot instance taken back hours into a download would waste them.
    started = fleet.start(
        launch,
        template,
        image=build.image,
        once=True,
        token=token,
        markets=[False],
        data=disks(build.spec["volumes"], setup=True),
        setup=str(build.pk),
    )
    if not started:
        launch.refresh_from_db(fields=["message", "ended_reason"])
        if launch.ended_reason == "no_capacity":
            SetupBuild.objects.filter(pk=build.pk, state=SetupState.QUEUED).update(
                progress_message=launch.message[:500]
            )
    logger.info("setup_launched", build=build.hrid, launch=str(launch.pk), started=started)
    return 1


def _record_disks(region: Region, build: SetupBuild) -> None:
    """Records which disk is each volume's on the build's machine, once EC2 lists them, and
    tags each with its volume."""
    launch = Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).first()
    if launch is None or sorted(launch.volumes) == sorted(build.spec["volumes"]):
        return
    instance = next((i for i in region.instances if _tag(i, LAUNCH_TAG) == str(launch.pk)), None)
    if instance is None:
        return
    attached = {
        str(m.get("DeviceName")): str(m["Ebs"]["VolumeId"])
        for m in instance.get("BlockDeviceMappings") or []
        if (m.get("Ebs") or {}).get("VolumeId")
    }
    found: dict[str, dict[str, Any]] = {}
    on_disks = [
        v
        for v in sorted(build.spec["volumes"])
        if not setups.shared(build, v) or setups.staged(build, v)
    ]
    for n, volume in enumerate(on_disks):
        disk = attached.get(DEVICES[n])
        if disk is None:
            return  # not attached yet
        found[volume] = {"disk": disk}
    for volume, recorded in found.items():
        region.ec2.tag(region.name, recorded["disk"], {VOLUME_TAG: volume}, [])
        if setups.staged(build, volume):
            # Recorded before anything's copied into the bucket, so a failed build's
            # partial copy is removed.
            volumes.record_shared(build, volume)
    for volume, declared in build.spec["volumes"].items():
        if setups.shared(build, volume) and not setups.staged(build, volume):
            found[volume] = {
                "filesystem": declared["filesystem"],
                "directory": declared["directory"],
            }
            volumes.record_shared(build, volume)
    Launch.objects.filter(pk=launch.pk).update(volumes=found)
    # Its worker may lease the build now.
    queue.notify("vm")
    logger.info("setup_disks_found", build=build.hrid, disks=found)


# --- Snapshotting what it filled ------------------------------------------------------------


def _snapshot(region: Region, build: SetupBuild, now: datetime) -> int:
    if Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).exists():
        return 0  # its machine still has the disks attached
    template = volumes.template_of(build)
    if template is None:
        return 1
    # The machine whose worker filled the disks recorded them before it leased the build.
    filler = next(
        (
            each
            for each in Launch.objects.filter(setup=build).order_by("-created_at")
            if each.volumes
        ),
        None,
    )
    recorded = dict(filler.volumes) if filler is not None else {}
    ready = 0
    failure = ""
    copying = False
    for volume, declared in sorted(build.spec["volumes"].items()):
        if setups.staged(build, volume):
            published, why = _published(region, build, volume, filler, now)
            ready += published
            copying = copying or not (published or why)
            failure = failure or why
            continue
        if setups.shared(build, volume):
            volumes.shared_ready(build, volume, now)
            ready += 1
            continue
        record = ManagedVolume.objects.filter(build=build, name=volume).first()
        snapshot = region.snapshot_of(build, volume)
        if snapshot is None:
            if record is None:
                failure = _take(region, build, volume, int(declared["size"]), recorded) or failure
            elif now - record.created_at > LIST_LAG:
                failure = f"Its snapshot of {volume} ({record.handle}) is gone."
            # EC2 lists a new snapshot a little after it says it took it.
            continue
        if record is None:
            record = ManagedVolume.objects.create(
                build=build,
                environment=build.environment,
                name=volume,
                place=build.place,
                handle=str(snapshot["SnapshotId"]),
                size=int(declared["size"]),
            )
        state = str(snapshot.get("State", ""))
        if state == "error":
            failure = f"Its snapshot of {volume} failed: {snapshot.get('StateMessage') or state}"
            continue
        if state != "completed":
            _say(record, f"{snapshot.get('Progress') or '0%'} copied")
            continue
        if not _fast(region, template, volume, record):
            continue
        if record.state != VolumeState.READY:
            record.state = VolumeState.READY
            record.ready_at = now
            record.message = ""
            record.save(update_fields=["state", "ready_at", "message"])
        ready += 1
    if (
        not failure
        and not copying
        and ready < len(build.spec["volumes"])
        and build.finished_at is None
        and volumes.stuck(build, now, SNAPSHOT_WITHIN)
    ):
        failure = "Its snapshots didn't become ready in time."
    if failure:
        volumes.fail(build, SetupState.SNAPSHOTTING, "snapshot_failed", failure)
        return 1
    if ready < len(build.spec["volumes"]):
        return 0
    # Its disks have served their purpose.
    for disk in recorded.values():
        if "disk" in disk:
            region.ec2.delete_volume(region.name, disk["disk"])
    return volumes.succeed(build)


def copying(build_id: str | None) -> bool:
    """Whether the setup build's machine may still be copying its disks into a bucket,
    though its launch has ended: it's left to finish."""
    if not build_id:
        return False
    try:
        build = SetupBuild.objects.filter(pk=uuid.UUID(build_id)).first()
    except ValueError:
        return False
    return (
        build is not None
        and build.state == SetupState.SNAPSHOTTING
        and any(setups.staged(build, v) for v in build.spec["volumes"])
    )


def _published(
    region: Region, build: SetupBuild, volume: str, filler: Launch | None, now: datetime
) -> tuple[int, str]:
    """Whether the build's machine copied its disk for the volume into the version's
    directory in the bucket: 1 once its console says it has, or why it couldn't."""
    if ManagedVolume.objects.filter(build=build, name=volume, state=VolumeState.READY).exists():
        return 1, ""
    if filler is None or not filler.machine:
        return 0, f"Its machine didn't fill a disk for {volume}."
    instance = next((i for i in region.instances if i["InstanceId"] == filler.machine), None)
    if instance is not None and _state(instance) not in ENDED:
        # Still copying, though its worker has gone.
        if filler.ended_at is not None and now - filler.ended_at > PUBLISH_WITHIN:
            return 0, f"Its machine took too long to copy {volume} into its bucket."
        return 0, ""
    said = region.ec2.console(region.name, filler.machine)
    if re.search(rf"^{PUBLISHED} {re.escape(volume)}\r?$", said, re.MULTILINE):
        volumes.shared_ready(build, volume, now)
        logger.info("setup_published", build=build.hrid, volume=volume)
        return 1, ""
    trouble = host_trouble(said)
    if trouble is not None:
        return 0, f"Iki couldn't copy {volume} into its bucket: {trouble[1]}"
    if filler.ended_at is not None and now - filler.ended_at < CONSOLE_WITHIN:
        return 0, ""  # EC2 posts its console a little after it ends
    return 0, f"Its machine didn't say it copied {volume} into its bucket."


def _take(
    region: Region, build: SetupBuild, volume: str, size: int, recorded: dict[str, Any]
) -> str:
    """Starts a snapshot of the volume's disk, once it's detached. Returns why it can't be
    taken, if it can't."""
    disk = (recorded.get(volume) or {}).get("disk")
    if not disk:
        return f"Iki didn't learn which disk was {volume}'s."
    found = region.volumes().get(disk)
    if found is None:
        return f"Its disk for {volume} ({disk}) is gone."
    if found.get("State") != "available":
        return ""  # still detaching from its instance
    snapshot = region.ec2.create_snapshot(
        region.name,
        disk,
        f"{build.environment} {volume}, version {build.version} ({build.hrid})",
        {
            OWNER_TAG: region.fleet.machines.owner,
            SETUP_TAG: str(build.pk),
            VOLUME_TAG: volume,
            ENVIRONMENT_TAG: build.environment,
            "Name": f"{build.hrid} {volume}",
        },
    )
    ManagedVolume.objects.get_or_create(
        build=build,
        name=volume,
        defaults={
            "environment": build.environment,
            "place": build.place,
            "handle": str(snapshot["SnapshotId"]),
            "size": size,
        },
    )
    logger.info("setup_snapshot_started", build=build.hrid, snapshot=snapshot["SnapshotId"])
    return ""


def _say(record: ManagedVolume, message: str) -> None:
    if record.message != message:
        record.message = message[:500]
        record.save(update_fields=["message"])


def _fast(region: Region, template: Template, volume: str, record: ManagedVolume) -> bool:
    """Whether the volume's completed snapshot is ready for runs: made fast to restore in
    each of the template's zones, if the template asks for that."""
    given = template.volumes.get(volume)
    if given is None or not given.fast_snapshot_restore:
        return True
    zones = sorted(set(region.fleet.zones(template).values()))
    states = {
        str(f["AvailabilityZone"]): str(f["State"])
        for f in region.ec2.fast_restores(region.name, [record.handle])
    }
    missing = [z for z in zones if states.get(z) not in FAST]
    if missing:
        refused = region.ec2.enable_fast_restores(region.name, record.handle, missing)
        if refused:
            _say(
                record,
                "EC2 wouldn't yet make it fast to restore: "
                + "; ".join(sorted(set(refused)))
                + ". Iki keeps asking: an account can have only so many snapshots fast to "
                "restore in a region.",
            )
            return False
        for zone in missing:
            states[zone] = "enabling"
    waiting = [z for z in zones if states.get(z) != "enabled"]
    if waiting:
        _say(
            record,
            "Being made fast to restore: "
            + ", ".join(f"{z} ({states.get(z, 'enabling')})" for z in waiting),
        )
        return False
    return True


# --- Tidying up -------------------------------------------------------------------------------


def _tidy(region: Region, now: datetime) -> None:
    """Deletes disks once their build is done with them, and snapshots once nothing will
    copy them again; keeps only the versions machines get now fast to restore; and removes
    versions from shared file systems once nothing will mount them again."""
    builds: dict[str, SetupBuild | None] = {}

    def build_of(item: dict[str, Any]) -> SetupBuild | None:
        key = _tag(item, SETUP_TAG) or ""
        if key not in builds:
            try:
                builds[key] = SetupBuild.objects.filter(pk=uuid.UUID(key)).first()
            except ValueError:  # not a build's ID
                builds[key] = None
        return builds[key]

    for disk, found in region.volumes().items():
        if found.get("State") != "available":
            continue  # attached to a machine, or on its way
        build = build_of(found)
        if build is not None and build.state in (SetupState.RUNNING, SetupState.SNAPSHOTTING):
            recorded = {
                d["disk"]
                for launch in Launch.objects.filter(setup=build)
                for d in launch.volumes.values()
                if isinstance(d, dict) and "disk" in d
            }
            if disk in recorded:
                continue
        region.ec2.delete_volume(region.name, disk)
        logger.info("setup_disk_deleted", disk=disk)
    kept: list[tuple[SetupBuild, ManagedVolume]] = []
    for snapshot in region.snapshots():
        build = build_of(snapshot)
        handle = str(snapshot["SnapshotId"])
        if build is not None and build.state in UNFINISHED:
            continue
        if build is not None and volumes.kept(build):
            record = ManagedVolume.objects.filter(build=build, handle=handle).first()
            if record is not None:
                kept.append((build, record))
                continue
            # One it took twice: the other is the version's.
        region.ec2.delete_snapshot(region.name, handle)
        logger.info("setup_snapshot_deleted", snapshot=handle)
        if build is not None:
            ManagedVolume.objects.filter(build=build, handle=handle).exclude(
                state=VolumeState.DELETED
            ).update(
                state=VolumeState.DELETED
                if build.state == SetupState.SUCCEEDED
                else VolumeState.FAILED,
                deleted_at=timezone.now(),
            )
    _only_current_fast(region, kept)
    _remove_shared(region, now)


def _only_current_fast(region: Region, kept: list[tuple[SetupBuild, ManagedVolume]]) -> None:
    """Keeps each version machines get now fast to restore, if its template asks, and no
    other: being fast costs by the hour, and new machines get only the current version."""
    if not kept:
        return
    states: dict[str, dict[str, str]] = {}
    for item in region.ec2.fast_restores(region.name, [r.handle for _, r in kept]):
        zones = states.setdefault(str(item["SnapshotId"]), {})
        zones[str(item["AvailabilityZone"])] = str(item["State"])
    current: dict[tuple[str, str], SetupBuild | None] = {}
    for build, record in kept:
        key = (build.environment, build.place)
        if key not in current:
            current[key] = setups.current(*key)
        now = current[key]
        template = catalog.compute().templates.get(build.template)
        given = template.volumes.get(record.name) if template is not None else None
        if (
            template is not None
            and given is not None
            and given.fast_snapshot_restore
            and now is not None
            and now.pk == build.pk
        ):
            # Current again, its successor retired: fast again, saying so till it is.
            if _fast(region, template, record.name, record):
                _say(record, "")
            continue
        fast = sorted(z for z, state in states.get(record.handle, {}).items() if state in FAST)
        if fast:
            region.ec2.disable_fast_restores(region.name, record.handle, fast)
            logger.info("setup_fast_restore_disabled", snapshot=record.handle, zones=fast)


def _state(instance: dict[str, Any]) -> str:
    return str((instance.get("State") or {}).get("Name", ""))


def _remove_shared(region: Region, now: datetime) -> None:
    """For each build with versions on shared file systems to remove, a small machine that
    removes them and says so on its console (ADR 0077). Once one has, they're deleted; one
    that didn't is started again :data:`CLEANUP_AGAIN` after the last."""
    for build, records in volumes.to_remove(region.name).items():
        mine = sorted(
            (i for i in region.instances if _tag(i, REMOVAL_TAG) == str(build.pk)),
            key=lambda i: i["LaunchTime"],
        )
        live = [i for i in mine if _state(i) not in ENDED]
        if live:
            late = [i["InstanceId"] for i in live if now - i["LaunchTime"] > CLEANUP_WITHIN]
            if late:
                region.ec2.terminate(region.name, late)
                _tell(records, "Its machine to remove them took too long. Iki tries again.")
            continue
        if mine:
            said = region.ec2.console(region.name, mine[-1]["InstanceId"])
            if REMOVED in said:
                volumes.removed(build, records)
                logger.info("setup_directories_removed", build=build.hrid)
                continue
            trouble = host_trouble(said)
            if trouble is not None:
                _tell(records, f"Iki couldn't remove its directory: {trouble[1]}. It tries again.")
            if now - mine[-1]["LaunchTime"] < CLEANUP_AGAIN:
                continue  # its console may say yet
        _clean_up(region, build, records, now)


def _tell(records: list[ManagedVolume], message: str) -> None:
    for record in records:
        _say(record, message)


def _clean_up(
    region: Region, build: SetupBuild, records: list[ManagedVolume], now: datetime
) -> None:
    template = catalog.compute().templates.get(build.template)
    if template is None:
        _tell(
            records,
            f"Its compute template, {build.template}, is gone, so Iki can't start a machine "
            "to remove its directory. Restore the template, or remove the directory yourself.",
        )
        return
    versions = [
        version
        for each in disks({r.name: build.spec["volumes"][r.name] for r in records}, setup=True)
        for version in [each.publish if isinstance(each, Disk) else each]
        if version is not None
    ]
    name = f"iki-cleanup-{build.hrid.lower()}"
    for subnet in template.aws["subnets"]:
        request = cleanup_request(
            build_id=str(build.pk),
            name=name,
            template=template,
            environment=build.environment,
            machines=region.fleet.machines,
            subnet=subnet,
            # A new try every so often; EC2 starts one machine for each, however often asked.
            attempt=int(now.timestamp()) // int(CLEANUP_AGAIN.total_seconds()),
            versions=versions,
        )
        try:
            started = region.ec2.run_instance(region.name, request)
        except Ec2Error as error:
            if is_capacity(error):
                continue  # another zone may have one
            _tell(records, f"EC2 wouldn't start a machine to remove its directory: {error}")
            return
        logger.info("setup_cleanup_started", build=build.hrid, instance=started.get("InstanceId"))
        return
    _tell(records, "EC2 had no machine to remove its directory with. Iki tries again.")
