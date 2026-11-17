"""Environment setup builds on Compute Engine, and the images they leave (ADR 0076, 0107):
the twin of :mod:`iki_server.runs.ec2_volumes`.

Each round, for each build in a region the deployment's Compute Engine templates use:

- **Queued:** a standard instance of the build's template, as a run's machine is started,
  with an empty, encrypted persistent disk for each volume the setup fills, kept when the
  instance is deleted (``autoDelete`` false). Once Compute Engine lists the disks attached
  to it, each is recorded on the launch; only then may its worker lease the build, so every
  disk it writes can be found again.
- **Snapshotting:** once the instance is gone and its disks are detached (so nothing is
  still writing), an image of each, in the template's region, encrypted with the
  deployment's key. Runs' machines get a new disk from it: an image, not a snapshot, as
  Compute Engine makes at most six disks an hour from one snapshot in a zone, and any
  number from an image. Once every image is ready, the build has succeeded and its disks
  are deleted.
- **Ended otherwise:** its disks, and any images, are deleted.
- **Succeeded:** a retired version's images are deleted once no live machine has a copy.

Disks and images are labelled ``iki-setup`` (their build's ID) and ``iki-volume``, besides
the deployment's own, and named for the deployment (``<owner>-…``); any whose build is
gone are deleted. Compute Engine has no fast snapshot restore: a template can't ask for it.

Shared file systems (ADR 0077) and buckets (ADR 0078) aren't yet supported on Compute
Engine; a template can't give a volume from one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from django.utils import timezone

from iki_server.runs import launches, queue, setups, volumes
from iki_server.runs.ec2 import disks
from iki_server.runs.gce import (
    DEVICE_NAMES,
    LAUNCH_LABEL,
    SETUP_LABEL,
    VOLUME_LABEL,
    GceError,
    image_request,
    label,
)
from iki_server.runs.models import (
    Launch,
    LaunchState,
    ManagedVolume,
    SetupBuild,
    SetupState,
    VolumeState,
)

if TYPE_CHECKING:
    from iki_server.runs.gce_fleet import GceFleet
    from iki_spec.compute import Template

__all__ = ["tick"]

logger = structlog.get_logger("iki.provisioner")

#: How long past its script's timeout a build's images may take before it fails: Compute
#: Engine copies a terabyte-sized disk into an image in hours.
SNAPSHOT_WITHIN = timedelta(hours=24)
#: Compute Engine may list a new image a little after it says it's making it.
LIST_LAG = timedelta(minutes=2)
#: How often to look for disks and images nothing needs.
TIDY_EVERY = timedelta(minutes=5)
UNFINISHED = (SetupState.QUEUED, SetupState.RUNNING, SetupState.SNAPSHOTTING)
#: Disk statuses that mean it's still being made, or is going.
BUSY = ("CREATING", "RESTORING", "DELETING")
#: The key :attr:`GceFleet.tidied` remembers the last tidy by: disks and images are listed
#: for the whole project at once.
EVERYWHERE = "*"


def _label(item: dict[str, Any], key: str) -> str | None:
    value = (item.get("labels") or {}).get(key)
    return None if value is None else str(value)


class Listing:
    """What Compute Engine says the deployment has, listed at most once a round."""

    def __init__(self, fleet: GceFleet, instances: list[dict[str, Any]]) -> None:
        self.fleet = fleet
        self.instances = instances
        self._disks: dict[tuple[str, str], dict[str, Any]] | None = None
        self._images: list[dict[str, Any]] | None = None

    @property
    def gce(self) -> Any:  # noqa: ANN401 - the fleet's Gce
        return self.fleet.gce

    @property
    def project(self) -> str:
        return self.fleet.project

    def disks(self) -> dict[tuple[str, str], dict[str, Any]]:
        """The deployment's setup builds' disks, by their zone and name."""
        if self._disks is None:
            listed = self.gce.disks(self.project, self.fleet.machines.owner)
            self._disks = {
                (str(d["zone"]), str(d["name"])): d
                for d in listed
                if _label(d, SETUP_LABEL) is not None
            }
        return self._disks

    def images(self) -> list[dict[str, Any]]:
        if self._images is None:
            self._images = list(self.gce.images(self.project, self.fleet.machines.owner))
        return self._images

    def image_of(self, build: SetupBuild, volume: str) -> dict[str, Any] | None:
        """The build's image of `volume`: the one Iki recorded, or failing that the first it
        made (in case it made one it couldn't record)."""
        record = ManagedVolume.objects.filter(build=build, name=volume).first()
        mine = [
            i
            for i in self.images()
            if _label(i, SETUP_LABEL) == str(build.pk) and _label(i, VOLUME_LABEL) == label(volume)
        ]
        if record is not None:
            return next((i for i in mine if handle(self.project, i) == record.handle), None)
        return min(mine, key=lambda i: str(i.get("creationTimestamp", "")), default=None)


def handle(project: str, image: dict[str, Any]) -> str:
    """How a version's image is recorded, and how a machine's disk names it."""
    return f"projects/{project}/global/images/{image['name']}"


def tick(fleet: GceFleet, instances: list[dict[str, Any]], regions: set[str], now: datetime) -> int:
    """One round for every build in `regions`, given the deployment's `instances`. Returns
    how many builds it moved on."""
    listing = Listing(fleet, instances)
    moved = 0
    builds = SetupBuild.objects.filter(place__in=regions, state__in=UNFINISHED)
    for build in builds.order_by("created_at"):
        try:
            if build.state == SetupState.QUEUED:
                _record_disks(listing, build)
                moved += _start(fleet, build, now)
            elif build.state == SetupState.SNAPSHOTTING:
                moved += _capture(listing, build, now)
        except GceError as error:
            logger.warning("setup_tick_failed", build=build.hrid, error=str(error))
    last = fleet.tidied.get(EVERYWHERE)
    if last is None or now - last >= TIDY_EVERY:
        try:
            _tidy(listing)
            fleet.tidied[EVERYWHERE] = now
        except GceError as error:
            logger.warning("setup_tidy_failed", project=fleet.project, error=str(error))
    return moved


# --- Starting a build's machine -------------------------------------------------------------


def _start(fleet: GceFleet, build: SetupBuild, now: datetime) -> int:
    if not volumes.ready_to_start(build, now):
        build.refresh_from_db(fields=["state"])
        return int(build.state != SetupState.QUEUED)
    template = volumes.template_of(build)
    if template is None:
        return 1
    mounted = sorted(v for v in build.spec["volumes"] if setups.shared(build, v))
    if mounted:
        volumes.fail(
            build,
            SetupState.QUEUED,
            "unsupported",
            f"Compute Engine machines can't yet fill volumes on shared file systems or in "
            f"buckets ({', '.join(mounted)}).",
        )
        return 1
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
    # Standard: a Spot VM taken back hours into a download would waste them.
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


def _record_disks(listing: Listing, build: SetupBuild) -> None:
    """Records which disk is each volume's on the build's machine, once Compute Engine lists
    them attached."""
    launch = Launch.objects.filter(setup=build).exclude(state=LaunchState.ENDED).first()
    if launch is None or sorted(launch.volumes) == sorted(build.spec["volumes"]):
        return
    instance = next(
        (i for i in listing.instances if _label(i, LAUNCH_LABEL) == str(launch.pk)), None
    )
    if instance is None:
        return
    attached = {
        str(d.get("deviceName")): str(d["source"])
        for d in instance.get("disks") or []
        if d.get("source") and not d.get("boot")
    }
    found: dict[str, dict[str, Any]] = {}
    for n, volume in enumerate(sorted(build.spec["volumes"])):
        source = attached.get(DEVICE_NAMES[n])
        if source is None:
            return  # not attached yet
        found[volume] = {"disk": source.rsplit("/", 1)[-1], "zone": str(instance["zone"])}
    Launch.objects.filter(pk=launch.pk).update(volumes=found)
    # Its worker may lease the build now.
    queue.notify("vm")
    logger.info("setup_disks_found", build=build.hrid, disks=found)


# --- Making images of what it filled -----------------------------------------------------------


def _capture(listing: Listing, build: SetupBuild, now: datetime) -> int:
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
    for n, (volume, declared) in enumerate(sorted(build.spec["volumes"].items())):
        record = ManagedVolume.objects.filter(build=build, name=volume).first()
        image = listing.image_of(build, volume)
        if image is None:
            if record is None:
                taken = _take(listing, build, template, n, volume, int(declared["size"]), recorded)
                failure = taken or failure
            elif now - record.created_at > LIST_LAG:
                failure = f"Its image of {volume} ({record.handle}) is gone."
            continue
        if record is None:
            record = ManagedVolume.objects.create(
                build=build,
                environment=build.environment,
                name=volume,
                place=build.place,
                handle=handle(listing.project, image),
                size=int(declared["size"]),
            )
        status = str(image.get("status", ""))
        if status == "FAILED":
            failure = f"Compute Engine couldn't make an image of {volume}."
            continue
        if status != "READY":
            _say(record, "Being copied into an image")
            continue
        if record.state != VolumeState.READY:
            record.state = VolumeState.READY
            record.ready_at = now
            record.message = ""
            record.save(update_fields=["state", "ready_at", "message"])
        ready += 1
    if (
        not failure
        and ready < len(build.spec["volumes"])
        and build.finished_at is None
        and volumes.stuck(build, now, SNAPSHOT_WITHIN)
    ):
        failure = "Its images didn't become ready in time."
    if failure:
        volumes.fail(build, SetupState.SNAPSHOTTING, "snapshot_failed", failure)
        return 1
    if ready < len(build.spec["volumes"]):
        return 0
    # Its disks have served their purpose.
    for disk in recorded.values():
        if "disk" in disk:
            listing.gce.delete_disk(listing.project, disk["zone"], disk["disk"])
    return volumes.succeed(build)


def _take(
    listing: Listing,
    build: SetupBuild,
    template: Template,
    n: int,
    volume: str,
    size: int,
    recorded: dict[str, Any],
) -> str:
    """Starts making an image of the volume's disk, once it's detached. Returns why it
    can't be made, if it can't."""
    disk = recorded.get(volume) or {}
    if not disk.get("disk"):
        return f"Iki didn't learn which disk was {volume}'s."
    found = listing.disks().get((disk["zone"], disk["disk"]))
    if found is None:
        return f"Its disk for {volume} ({disk['disk']}) is gone."
    if found.get("users") or found.get("status") in BUSY:
        return ""  # still detaching from its instance
    fleet = listing.fleet
    request = image_request(
        build_id=str(build.pk),
        name=f"{fleet.machines.owner}-{label(build.hrid)}-v{n + 1}",
        description=f"{build.environment} {volume}, version {build.version} ({build.hrid})",
        disk=f"projects/{fleet.project}/zones/{disk['zone']}/disks/{disk['disk']}",
        region=str(template.gcp["region"]),
        kms_key=fleet.kms_key(template),
        machines=fleet.machines,
        environment=build.environment,
        volume=volume,
    )
    listing.gce.create_image(listing.project, request)
    ManagedVolume.objects.get_or_create(
        build=build,
        name=volume,
        defaults={
            "environment": build.environment,
            "place": build.place,
            "handle": handle(listing.project, request["image"]),
            "size": size,
        },
    )
    logger.info("setup_image_started", build=build.hrid, image=request["image"]["name"])
    return ""


def _say(record: ManagedVolume, message: str) -> None:
    if record.message != message:
        record.message = message[:500]
        record.save(update_fields=["message"])


# --- Tidying up -------------------------------------------------------------------------------


def _tidy(listing: Listing) -> None:
    """Deletes disks once their build is done with them, and images once nothing will copy
    them again."""
    builds: dict[str, SetupBuild | None] = {}

    def build_of(item: dict[str, Any]) -> SetupBuild | None:
        key = _label(item, SETUP_LABEL) or ""
        if key not in builds:
            try:
                builds[key] = SetupBuild.objects.filter(pk=uuid.UUID(key)).first()
            except ValueError:  # not a build's ID
                builds[key] = None
        return builds[key]

    for (zone, name), found in listing.disks().items():
        if found.get("users") or found.get("status") in BUSY:
            continue  # attached to a machine, or on its way
        build = build_of(found)
        if build is not None and build.state in UNFINISHED:
            recorded = {
                (d.get("zone"), d.get("disk"))
                for launch in Launch.objects.filter(setup=build)
                for d in launch.volumes.values()
                if isinstance(d, dict)
            }
            if (zone, name) in recorded:
                continue
        listing.gce.delete_disk(listing.project, zone, name)
        logger.info("setup_disk_deleted", disk=name, zone=zone)
    for image in listing.images():
        build = build_of(image)
        made = handle(listing.project, image)
        if build is not None and build.state in UNFINISHED:
            continue
        if (
            build is not None
            and volumes.kept(build)
            and ManagedVolume.objects.filter(build=build, handle=made).exists()
        ):
            continue
        # Its build is done with it, or it's one made twice (the other is the version's).
        listing.gce.delete_image(listing.project, str(image["name"]))
        logger.info("setup_image_deleted", image=made)
        if build is not None:
            ManagedVolume.objects.filter(build=build, handle=made).exclude(
                state=VolumeState.DELETED
            ).update(
                state=VolumeState.DELETED
                if build.state == SetupState.SUCCEEDED
                else VolumeState.FAILED,
                deleted_at=timezone.now(),
            )
