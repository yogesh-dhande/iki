"""Compute admin: environments' volumes, and the setup builds that fill them (ADR 0075).

All of it takes the Compute admin role. A build whose environment asks for approval waits
for another Compute admin's, never its requester's.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import caller, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.entities import registry
from iki_server.org.permissions import Action
from iki_server.runs import setups
from iki_server.runs.models import (
    ManagedVolume,
    SetupBuild,
    SetupEgress,
    SetupLog,
)

router = Router(tags=["compute"])

SetupStateName = Literal[
    "awaiting_approval",
    "queued",
    "running",
    "snapshotting",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]


class VolumeOut(Schema):
    name: str
    size: int = Field(description="Bytes.")
    mount: str
    access: str
    filled_by_setup: bool


class SetupOut(Schema):
    script: str
    timeout: int | None = Field(description="Seconds.")
    requires_approval: bool
    network: list[str] = Field(description="The hosts the setup may reach.")


class VersionOut(Schema):
    place: str = Field(description="Where its runs go: a Kubernetes namespace, or an AWS region.")
    build: UUID | None = Field(description="The setup build whose version runs get there now.")
    hrid: str | None
    version: int | None
    data_version: str | None
    ready_at: datetime | None


class EnvironmentVolumesOut(Schema):
    environment: str = Field(description="plugin.environment")
    label: str
    volumes: list[VolumeOut]
    setup: SetupOut | None
    places: list[VersionOut] = Field(
        description="Where its runs go, and the version machines there get."
    )
    waiting: str = Field(description="Why its runs can't start for want of its volumes, if so.")


@router.get(
    "/compute/volumes",
    response=list[EnvironmentVolumesOut],
    summary="Environments' volumes and their versions",
)
def list_volumes(request: HttpRequest) -> list[dict[str, Any]]:
    """Each installed environment that declares volumes."""
    require(caller(request), Action.COMPUTE_MANAGE)
    found = []
    for plugin_name, plugin in sorted(registry.plugins().items()):
        for name, environment in sorted(plugin.environments.items()):
            if not environment.volumes:
                continue
            qualified = f"{plugin_name}.{name}"
            versions = []
            for place in setups.places(qualified):
                build = setups.current(qualified, place.name)
                versions.append(
                    {
                        "place": place.name,
                        "build": build.pk if build else None,
                        "hrid": build.hrid if build else None,
                        "version": build.version if build else None,
                        "data_version": build.data_version if build else None,
                        "ready_at": build.finished_at if build else None,
                    }
                )
            found.append(
                {
                    "environment": qualified,
                    "label": environment.label or name,
                    "volumes": [
                        {
                            "name": v.name,
                            "size": v.size,
                            "mount": v.mount,
                            "access": v.access,
                            "filled_by_setup": v.populated_by == "setup",
                        }
                        for v in sorted(environment.volumes.values(), key=lambda v: v.name)
                    ],
                    "setup": {
                        "script": environment.setup_script,
                        "timeout": environment.setup_timeout,
                        "requires_approval": environment.setup_requires_approval,
                        "network": list(environment.setup_network),
                    }
                    if environment.setup_script
                    else None,
                    "places": versions,
                    "waiting": setups.missing(qualified),
                }
            )
    return found


# --- Setup builds -------------------------------------------------------------------------------


class SetupErrorOut(Schema):
    code: str = Field(
        description="`failed`, `timed_out`, `interrupted`, `cancelled`, `worker_lost`, "
        "`machine_unavailable`, `template_gone` or `snapshot_failed`."
    )
    message: str


class SetupVolumeOut(Schema):
    name: str
    state: Literal["creating", "ready", "failed", "deleted"] = Field(
        description="`ready`: machines can get a copy of it, or mount it."
    )
    size: int = Field(description="Bytes.")
    kind: Literal["snapshot", "directory"] = Field(
        description="`snapshot`: machines get a copy of it. `directory`: its own directory on "
        "a shared file system, which machines mount read-only."
    )
    handle: str = Field(
        description="Where it is: its snapshot's name or ID, or its directory on a shared "
        "file system."
    )
    message: str


class SetupEgressOut(Schema):
    host: str
    port: int
    refused: str = Field(description="Why the proxy refused it; empty if it was allowed.")
    connections: int
    bytes_received: int


class SetupBuildOut(Schema):
    id: UUID
    hrid: str
    environment: str
    place: str
    template: str
    version: int
    state: SetupStateName
    reason: str
    image: str
    plugin_version: str
    plugin_sha: str
    requested_by: PersonRef
    approved_by: PersonRef | None
    approved_at: datetime | None
    cancel_requested_at: datetime | None
    data_version: str
    retired_at: datetime | None
    retired_by: PersonRef | None
    current: bool = Field(description="Whether machines for its environment's runs get it now.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    timeout: int = Field(description="Seconds its script may run.")
    progress: float | None
    progress_message: str
    log_size: int = Field(description="Bytes of log its script wrote.")
    error: SetupErrorOut | None
    volumes: list[SetupVolumeOut] = Field(description="The snapshot of each volume it filled.")
    egress: list[SetupEgressOut] = Field(
        description="The hosts its script reached, or was refused, through the egress proxy."
    )


def _builds() -> QuerySet[SetupBuild]:
    return SetupBuild.objects.select_related("requested_by", "approved_by", "retired_by")


def _out(build: SetupBuild) -> dict[str, Any]:
    current = setups.current(build.environment, build.place)
    return {
        **{
            field: getattr(build, field)
            for field in (
                "id",
                "hrid",
                "environment",
                "place",
                "template",
                "version",
                "state",
                "reason",
                "image",
                "plugin_version",
                "plugin_sha",
                "requested_by",
                "approved_by",
                "approved_at",
                "cancel_requested_at",
                "data_version",
                "retired_at",
                "retired_by",
                "created_at",
                "started_at",
                "finished_at",
                "timeout",
                "progress",
                "progress_message",
                "log_size",
                "error",
            )
        },
        "current": current is not None and current.pk == build.pk,
        "volumes": [
            {
                "name": v.name,
                "state": v.state,
                "size": v.size,
                "kind": "directory" if setups.shared(build, v.name) else "snapshot",
                "handle": v.handle,
                "message": v.message,
            }
            for v in ManagedVolume.objects.filter(build=build).order_by("name")
        ],
        "egress": [
            {
                "host": e.host,
                "port": e.port,
                "refused": e.refused,
                "connections": e.connections,
                "bytes_received": e.bytes_received,
            }
            for e in SetupEgress.objects.filter(build=build).order_by("host", "refused")
        ],
    }


def _build(ref: str) -> SetupBuild:
    try:
        found = _builds().filter(pk=UUID(ref)).first()
    except ValueError:
        found = _builds().filter(hrid=ref.upper()).first()
    if found is None:
        raise Problem(404, "No such setup build.")
    return found


class SetupFilters(PageParams):
    environment: str | None = Field(None, description="plugin.environment")
    state: SetupStateName | None = None


@router.get("/compute/setups", response=Page[SetupBuildOut], summary="Setup builds")
def list_setups(request: HttpRequest, filters: Query[SetupFilters]) -> dict[str, Any]:
    """Newest first."""
    require(caller(request), Action.COMPUTE_MANAGE)
    builds = _builds()
    if filters.environment:
        builds = builds.filter(environment=filters.environment)
    if filters.state:
        builds = builds.filter(state=filters.state)
    result = page(builds, filters, newest_first=True)
    result["items"] = [_out(b) for b in result["items"]]
    return result


class SetupIn(Schema):
    environment: str = Field(description="plugin.environment")
    reason: str = Field("", max_length=2000, description="Why: a new database release, say.")


@router.post(
    "/compute/setups", response={201: list[SetupBuildOut]}, summary="Ask for a setup build"
)
def create_setup(request: HttpRequest, payload: SetupIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """One build for each place the environment's runs go. Each waits for another Compute
    admin's approval if its setup asks for it, and is queued otherwise. 409 while one is
    still going there."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    if setups.environment_of(payload.environment) is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/environment", "detail": "No such environment."}],
        )
    made = setups.request(me, payload.environment, payload.reason)
    response["Location"] = f"/api/v1/compute/setups/{made[0].pk}"
    return Status(201, [_out(_build(str(b.pk))) for b in made])


@router.get("/compute/setups/{ref}", response=SetupBuildOut, summary="A setup build")
def get_setup(request: HttpRequest, ref: str) -> dict[str, Any]:
    """By its ID or HRID (SETUP-000012)."""
    require(caller(request), Action.COMPUTE_MANAGE)
    return _out(_build(ref))


@router.post(
    "/compute/setups/{ref}/approve", response=SetupBuildOut, summary="Approve a setup build"
)
def approve_setup(request: HttpRequest, ref: str) -> dict[str, Any]:
    """By a Compute admin other than whoever asked for it."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    return _out(_build(str(setups.approve(me, _build(ref).pk).pk)))


@router.post("/compute/setups/{ref}/cancel", response=SetupBuildOut, summary="Cancel a setup build")
def cancel_setup(request: HttpRequest, ref: str) -> dict[str, Any]:
    """At once unless its script is running; then once its worker has stopped it."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    return _out(_build(str(setups.cancel(me, _build(ref).pk).pk)))


@router.post(
    "/compute/setups/{ref}/retire",
    response=SetupBuildOut,
    summary="Retire a version of an environment's volumes",
)
def retire_setup(request: HttpRequest, ref: str) -> dict[str, Any]:
    """New machines get the newest version that isn't retired (or none, and its runs wait).
    Machines that have it keep it; its snapshots are deleted once none does."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    return _out(_build(str(setups.retire(me, _build(ref).pk).pk)))


class SetupLogOut(Schema):
    offset: int = Field(description="Where `text` starts, in bytes.")
    text: str
    size: int = Field(description="Bytes of log so far.")
    finished: bool = Field(description="Whether the build has ended: there'll be no more.")


@router.get("/compute/setups/{ref}/log", response=SetupLogOut, summary="A setup build's log")
def setup_log(request: HttpRequest, ref: str, offset: int = 0) -> dict[str, Any]:
    """What its script wrote, from byte `offset` on (the `size` you have), up to 1 MiB at
    a time."""
    require(caller(request), Action.COMPUTE_MANAGE)
    if offset < 0:
        raise Problem(
            422,
            "`offset` is a number of bytes.",
            errors=[{"parameter": "offset", "detail": "Use 0 or more."}],
        )
    build = _build(ref)
    chunks = SetupLog.objects.filter(build=build, offset__gte=_start(build, offset)).order_by(
        "offset"
    )
    text = b""
    start = offset
    for chunk in chunks:
        data = chunk.text.encode()
        if chunk.offset < offset:
            data = data[offset - chunk.offset :]
        elif not text:
            start = chunk.offset
        text += data
        if len(text) >= 1024**2:
            break
    return {
        "offset": start,
        "text": text.decode(errors="ignore"),
        "size": build.log_size,
        "finished": build.finished,
    }


def _start(build: SetupBuild, offset: int) -> int:
    """The offset of the chunk holding byte `offset`."""
    before = (
        SetupLog.objects.filter(build=build, offset__lte=offset)
        .order_by("-offset")
        .values_list("offset", flat=True)
        .first()
    )
    return before if before is not None else 0
