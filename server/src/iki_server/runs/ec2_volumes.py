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
- **Retired:** its snapshots stop being fast to restore, which costs by the hour, and are
  deleted once no live machine has a copy.

Disks and snapshots carry the tags ``iki.ikisci.org/setup`` (their build's ID) and
``iki.ikisci.org/volume``, besides the deployment's own; any whose build is gone are
deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from django.utils import timezone

from iki_server.runs import launches, queue, volumes
from iki_server.runs.ec2 import (
    DEVICES,
    ENVIRONMENT_TAG,
    LAUNCH_TAG,
    OWNER_TAG,
    SETUP_TAG,
    VOLUME_TAG,
    Ec2Error,
    disks,
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
                _tidy(region)
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
        data=disks(build.spec["volumes"]),
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
    wanted = sorted(build.spec["volumes"])
    if launch is None or sorted(launch.volumes) == wanted:
        return
    instance = next((i for i in region.instances if _tag(i, LAUNCH_TAG) == str(launch.pk)), None)
    if instance is None:
        return
    attached = {
        str(m.get("DeviceName")): str(m["Ebs"]["VolumeId"])
        for m in instance.get("BlockDeviceMappings") or []
        if (m.get("Ebs") or {}).get("VolumeId")
    }
    found: dict[str, dict[str, str]] = {}
    for n, volume in enumerate(wanted):
        disk = attached.get(DEVICES[n])
        if disk is None:
            return  # not attached yet
        found[volume] = {"disk": disk}
    for volume, recorded in found.items():
        region.ec2.tag(region.name, recorded["disk"], {VOLUME_TAG: volume}, [])
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
    for volume, declared in sorted(build.spec["volumes"].items()):
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
    if not failure and build.finished_at is None and volumes.stuck(build, now, SNAPSHOT_WITHIN):
        failure = "Its snapshots didn't become ready in time."
    if failure:
        volumes.fail(build, SetupState.SNAPSHOTTING, "snapshot_failed", failure)
        return 1
    if ready < len(build.spec["volumes"]):
        return 0
    # Its disks have served their purpose.
    for disk in recorded.values():
        region.ec2.delete_volume(region.name, disk["disk"])
    return volumes.succeed(build)


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
                + ". Iki keeps asking; retiring an old version frees its fast restores.",
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


def _tidy(region: Region) -> None:
    """Deletes disks once their build is done with them, and snapshots once nothing will
    copy them again; stops retired snapshots being fast to restore."""
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
    retired: list[str] = []
    for snapshot in region.snapshots():
        build = build_of(snapshot)
        handle = str(snapshot["SnapshotId"])
        if build is not None and build.state in UNFINISHED:
            continue
        if build is not None and volumes.kept(build):
            record = ManagedVolume.objects.filter(build=build, handle=handle).first()
            if record is not None:
                if build.retired_at is not None:
                    retired.append(handle)
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
    # Being fast to restore costs by the hour; new machines no longer get these.
    fast: dict[str, list[str]] = {}
    for item in region.ec2.fast_restores(region.name, retired):
        if item.get("State") in FAST:
            fast.setdefault(str(item["SnapshotId"]), []).append(str(item["AvailabilityZone"]))
    for handle, zones in sorted(fast.items()):
        region.ec2.disable_fast_restores(region.name, handle, sorted(zones))
        logger.info("setup_fast_restore_disabled", snapshot=handle, zones=zones)
