"""Files: uploading them in parts, and reading and downloading them (ADR 0036).

To add a file:

1. ``POST /uploads`` with its name, media type, size and SHA-256. The answer
   lists its parts, each with a signed URL.
2. ``PUT`` each part's bytes to its URL. Parts can go in parallel, and in
   any order. URLs last an hour: ``POST /uploads/{id}/parts`` gives new ones.
3. ``POST /uploads/{id}/complete``. Iki checks the bytes against the SHA-256
   (state ``checking``) and scans them for malware (``scanning``). A completed
   upload is ``ready``, with its ``file``. Its ``scan.usable`` says whether
   it can be downloaded or attached to a record.
   ``upload.finished`` arrives on ``/events`` when it's done.

Or, for a file already in an approved bucket, ``POST /registrations`` with
its ``s3://`` URI: Iki pins its current version, reads it to learn its
SHA-256 and scans it. The registration then has its ``file`` and scan.
``registration.finished`` arrives on ``/events``.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Literal, Required, TypedDict
from uuid import UUID

from django.db.models import Count, F, Window
from django.db.models.functions import RowNumber
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require
from iki_server.api.pagination import PageParams, page, page_sorted
from iki_server.api.problems import Problem
from iki_server.api.runs import RunStatusName
from iki_server.api.schemas import AgentOut, FileScanOut, Page, ProjectRef, UserOut, agent_out
from iki_server.audit import log as audit
from iki_server.entities import access, registry
from iki_server.entities import services as entity_services
from iki_server.entities.models import Entity
from iki_server.entities.values import shape, to_json
from iki_server.files import acquired, listing, scan_state, services, storage
from iki_server.files import metadata as stated
from iki_server.files.models import (
    BlobScan,
    File,
    Registration,
    RegistrationState,
    Upload,
    UploadKind,
    UploadState,
)
from iki_server.files.scan_state import require_usable
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

router = Router(tags=["files"])


# --- Schemas -------------------------------------------------------------------------------------


class AcquiredOut(Schema):
    on: date = Field(description="The day, as its source gave it.")
    at: str = Field(
        description="When, in ISO 8601, as precisely as its source gave it: a date, or a date "
        "and time, with an offset only if the source stated one."
    )
    source: str = Field(
        description="Where that came from: ``given`` by whoever added it, or the format that "
        "records it in the file, such as ``abif`` or ``fcs``."
    )
    source_label: str = Field(description="Where that came from, as people would say it.")


class StatedOut(Schema):
    value: str = Field(description="As the file gives it.")
    stated_in: str = Field(
        description="Where in the file it's stated, such as ``FCS keyword $CYT``, to check it by."
    )


class FileFactsOut(Schema):
    """What the file says about where its data came from (ADR 0224), read when it was
    added once its scan let it be used: a held file's bytes aren't parsed. Each is null when
    the file doesn't say, or doesn't say clearly."""

    instrument: StatedOut | None = Field(
        description="The instrument's model, such as Aurora or Orbitrap Eclipse."
    )
    manufacturer: StatedOut | None = Field(description="Who made the instrument.")
    serial: StatedOut | None = Field(description="The instrument's serial number.")
    instrument_name: StatedOut | None = Field(
        description="The name the instrument was given, such as an AB1 trace's MCHN."
    )
    operator: StatedOut | None = Field(description="Who operated the instrument.")
    software: StatedOut | None = Field(
        description="The software that recorded the data, and its version where given."
    )
    protocol: StatedOut | None = Field(
        description="The instrument's protocol, such as a plate reader's test."
    )


class HeldCopyOut(Schema):
    """Where held bytes are: a project's file, or a run's or a bundle's own upload."""

    kind: Literal["file", "artifact", "checkpoint", "bundle"] = Field(
        description="A project's ``file``; or a run's ``artifact`` or ``checkpoint``, or a "
        "``bundle`` to import, each kept apart from the project's files."
    )
    file: UUID | None = Field(
        description="The project file, for a ``file``, if you can read it: its page opens."
    )
    name: str
    media_type: str
    project: ProjectRef
    run: str | None = Field(description="The run's ID, for an artifact or a checkpoint.")
    created_by: UserOut | None = Field(description="Who added it.")
    agent: AgentOut | None = Field(description="The agent that added it for them, if one did.")
    created_at: datetime


class QuarantineReleaseOut(Schema):
    by: UserOut = Field(description="The Quarantine Admin who released it.")
    at: datetime
    reason: str = Field(description="Why they found the finding false, as they gave it.")


class QuarantineOut(Schema):
    id: UUID = Field(description="The decision ID for releasing a confirmed false positive.")
    sha256: str
    size: int = Field(description="Bytes.")
    state: Literal["infected", "not_scanned"]
    scanner: str
    signature_version: str
    scanned_at: datetime
    reason: str = Field(
        description="What was found (the signature's name), or why an inspection could not "
        "finish: ``too_large``, ``unsupported_source``, ``clamd_stream_limit``, or the "
        "scanner's own reason, such as ``Heuristics.Encrypted.Zip``."
    )
    releasable: bool = Field(
        description="An infected finding not yet released. An incomplete inspection never is: "
        "it found nothing to call false (ADR 0217)."
    )
    release: QuarantineReleaseOut | None = Field(description="Its release, once released.")
    files: list[HeldCopyOut] = Field(
        description="Where the bytes are, oldest first: the first few in a list, every one in "
        "a decision's own."
    )
    file_count: int = Field(description="How many places the bytes are, listed or not.")


class QuarantineFilters(PageParams):
    released: bool = Field(
        False,
        description="Infected findings released as false positives, rather than what's held now.",
    )
    state: Literal["infected", "not_scanned"] | None = Field(
        None, description="Only malware findings, or only inspections that couldn't finish."
    )
    q: str = Field(
        "", max_length=255, description="Part of a file's name, or the start of its SHA-256."
    )


class ReleaseQuarantineIn(Schema):
    reason: str = Field(min_length=1, max_length=2000, description="Why the finding is false.")


class FileOut(Schema):
    id: UUID
    name: str
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str
    project: ProjectRef | None = Field(description="Where it was added. Null if you can't see it.")
    created_by: UserOut | None
    agent: AgentOut | None = Field(
        description="The agent that added it, acting for created_by, if one did (ADR 0072)."
    )
    created_at: datetime
    external: bool = Field(description="Registered where it already was, rather than uploaded.")
    scan: FileScanOut = Field(description="The deployment's malware scan of the file's bytes.")
    acquired: AcquiredOut | None = Field(
        description="When its data was acquired (ADR 0177): as its format records it, or as "
        "whoever added it said. Null if neither says: never when it was added."
    )
    facts: FileFactsOut = Field(
        description="Its instrument, serial number, operator, software and protocol, where "
        "its format records them (ADR 0224)."
    )


class UsedByCountOut(Schema):
    records: int = Field(description="Records you can read that hold it, at any revision.")
    runs: int = Field(description="Runs you can read that read or made it.")


class FileRowOut(FileOut):
    used_by: UsedByCountOut


class FilePageOut(Schema):
    items: list[FileRowOut]
    next_cursor: str | None = Field(
        description="Pass as cursor for the next page; null at the end."
    )


class RunRef(Schema):
    id: UUID
    hrid: str
    action: str = Field(description="Its action, qualified.")
    label: str = Field(description="Its action's label.")


class FileOriginOut(Schema):
    kind: Literal["upload", "run", "registration", "import"] = Field(
        description="How it was added: uploaded, uploaded by a run's worker for its results, "
        "registered where it already was, or imported with a bundle from another deployment."
    )
    run: RunRef | None = Field(description="The run that made it, if you can read the run.")
    uri: str | None = Field(description="Where a registered file is, such as s3://lab/x.csv.")
    version: str | None = Field(description="The object version a registered file is pinned to.")


class SampleOut(Schema):
    id: UUID
    hrid: str
    title: str
    type: str
    via_run: RunRef | None = Field(
        description="The run it came through: one that read the sample and read or made the "
        "file. Null when the sample holds the file itself."
    )


class ReadGroupOut(TypedDict, total=False):
    """A SAM or BAM file's @RG line: the SAM specification's tags it gives (section 1.3),
    by their two-letter names, such as ID, SM (sample), LB (library) and PU (platform unit)."""

    ID: Required[str]
    BC: str
    CN: str
    DS: str
    DT: str
    FO: str
    KS: str
    LB: str
    PG: str
    PI: str
    PL: str
    PM: str
    PU: str
    SM: str


class FileDetailOut(FileRowOut):
    read_groups: list[ReadGroupOut] = Field(
        description="A SAM or BAM file's read groups, each @RG line as it gives it (ADR "
        "0224): every one, or none when any can't be read as the specification defines."
    )
    origin: FileOriginOut
    samples: list[SampleOut] = Field(description="The samples it belongs to that you can read.")


class SpanOut(Schema):
    first: int = Field(description="The first revision that held it.")
    last: int | None = Field(description="The last that did; null if the latest still does.")
    fields: list[str] = Field(description="The fields that held it.")


class HoldingRecordOut(Schema):
    id: UUID
    hrid: str
    title: str
    type: str
    revision: int = Field(description="The record's latest revision.")
    spans: list[SpanOut] = Field(description="Its revisions that held the file.")


class UsingRunOut(Schema):
    id: UUID
    hrid: str
    action: str
    label: str
    status: RunStatusName
    project: ProjectRef
    roles: list[Literal["param", "input", "made"]] = Field(
        description="How it used the file: a file param, an input's file, or made by it."
    )
    created_at: datetime


class FormatOut(Schema):
    id: str | None = Field(description="The declared format ID, when installed.")
    label: str
    media_type: str
    count: int


class FileSignatureOut(Schema):
    offset: int
    hex: str


class InstalledFormatOut(Schema):
    id: str
    label: str
    canonical: str
    media_types: list[str] = Field(description="The canonical media type followed by its aliases.")
    extensions: list[str]
    signatures: list[FileSignatureOut]
    reader: str | None
    viewer: str | None
    parsers: list[str] = Field(description="Installed actions whose file inputs accept the format.")


class FileCountOut(Schema):
    total: int = Field(description="How many match, up to 1,000.")
    more_than_total: bool


class PartOut(Schema):
    number: int
    size: int = Field(description="Bytes this part must hold.")
    url: str = Field(description="PUT the part's bytes here. It works for an hour.")


class UploadStatusOut(Schema):
    """Your job status when the file is outside your current access."""

    id: UUID
    state: Literal["pending", "checking", "scanning", "ready", "failed", "cancelled", "expired"]
    created_at: datetime
    expires_at: datetime = Field(description="Unfinished then, it's cancelled.")
    file: FileOut | None = Field(description="The file it became, once ready and readable.")
    error: str = Field(description="Why it failed, if you can still read the details.")


class UploadOut(UploadStatusOut):
    project: UUID | None = Field(description="Where it was added. Null if you can't see it.")
    name: str
    media_type: str
    size: int
    sha256: str
    part_size: int
    part_count: int


# Preserve the published OpenAPI component name as the items gain a status-only form.
class Page_UploadOut_(Page[UploadOut | UploadStatusOut]):  # noqa: N801
    pass


class StartedOut(UploadOut):
    project: UUID
    parts: list[PartOut] = Field(description="Every part, with where to send it.")


class UploadIn(Schema):
    project: UUID = Field(description="The project to add it to. It needs you as an Editor.")
    name: str = Field(max_length=1000, description="Its file name, such as 1abc.pdb.")
    media_type: str = Field(max_length=255, description="Such as chemical/x-pdb.")
    size: int = Field(ge=0, description="Bytes.")
    sha256: str = Field(
        min_length=64, max_length=64, description="Of its bytes, as hex: Iki checks them."
    )
    acquired_at: str = Field(
        "",
        max_length=40,
        description="When its data was acquired, if you know: a date (2026-03-14) or a date "
        "and time (2026-03-14T09:21, with an offset if known), in ISO 8601. Iki keeps the "
        "file's own instead when its format records one (ADR 0177).",
    )


class PartsIn(Schema):
    numbers: list[int] | None = Field(
        None, max_length=1000, description="The parts to sign again; all of them if left out."
    )


class RegistrationIn(Schema):
    project: UUID = Field(description="The project to add it to. It needs you as an Editor.")
    uri: str = Field(
        max_length=2048, description="The object, such as s3://lab-data/runs/plate-7.csv."
    )
    version: str = Field(
        "", max_length=1024, description="The object version to pin; its current one if empty."
    )
    name: str = Field("", max_length=1000, description="Its name; the key's last part if empty.")
    media_type: str = Field(
        "", max_length=255, description="Such as text/csv; the object's own if empty."
    )
    acquired_at: str = Field(
        "",
        max_length=40,
        description="When its data was acquired, if you know, as for an upload (ADR 0177).",
    )


class RegistrationStatusOut(Schema):
    """Your job status when the file is outside your current access."""

    id: UUID
    state: Literal["checking", "scanning", "ready", "failed"]
    created_at: datetime
    file: FileOut | None = Field(description="The file it became, once ready and readable.")
    error: str = Field(description="Why it failed, if you can still read the details.")


class RegistrationOut(RegistrationStatusOut):
    project: UUID | None = Field(description="Where it was added. Null if you can't see it.")
    name: str
    media_type: str
    uri: str
    version: str = Field(description="The object version the file is pinned to.")
    size: int


class Page_RegistrationOut_(Page[RegistrationOut | RegistrationStatusOut]):  # noqa: N801
    pass


class RegistrationLocationOut(Schema):
    uri: str = Field(description="Where files may be registered, such as s3://lab/.")


_FILE_QUERY = (
    "Part of its name, or of what it states (its instrument, serial number, operator, "
    "software, protocol or read groups), in any case."
)


class FileFilters(PageParams):
    project: UUID | None = Field(None, description="Its files; every one you can read if left out.")
    q: str = Field("", max_length=255, description=_FILE_QUERY)
    format: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Format IDs, media types such as application/x-abi, or image/* for any image.",
    )
    acquired_from: date | None = Field(None, description="Acquired on this day or later.")
    acquired_to: date | None = Field(None, description="Acquired on this day or earlier.")
    sample: str | None = Field(
        None,
        max_length=64,
        description="A sample, by ID or HRID: the files it holds, and those a run that read it "
        "read or made (ADR 0177).",
    )
    run: str | None = Field(
        None, max_length=64, description="A run, by ID or HRID: the files it read or made."
    )
    accepts: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Only files a field or param takes: its media types, as `format`.",
    )
    max_size: int | None = Field(None, ge=0, description="Only files this many bytes or fewer.")
    sort: listing.Sort = Field(
        "newest",
        description="newest or oldest added, name (A to Z) or name_desc, or by when acquired: "
        "acquired (latest first) or acquired_oldest, unknown last either way.",
    )


class FacetParams(Schema):
    project: UUID | None = None
    q: str = Field("", max_length=255, description=_FILE_QUERY)
    acquired_from: date | None = None
    acquired_to: date | None = None
    sample: str | None = Field(None, max_length=64)
    run: str | None = Field(None, max_length=64)
    accepts: list[str] = Field(default_factory=list, max_length=50)
    max_size: int | None = Field(None, ge=0)
    format: list[str] = Field(default_factory=list, max_length=50)


# --- Helpers -------------------------------------------------------------------------------------


def _storage() -> None:
    if not storage.configured():
        raise Problem(503, str(storage.NotConfigured()), code="storage_not_configured")


def _file_out(user: User, files: list[File]) -> list[dict[str, Any]]:
    scans = scan_state.statuses_for_blobs({f.blob_id for f in files})
    projects = {
        p.pk: p
        for p in permissions.visible_projects(user).filter(pk__in={f.project_id for f in files})
    }
    return [
        {
            "id": f.pk,
            "name": f.name,
            "media_type": f.media_type,
            "size": f.blob.size,
            "sha256": f.blob.sha256,
            "project": projects.get(f.project_id),
            "created_by": f.created_by,
            "agent": agent_out(f.agent),
            "created_at": f.created_at,
            "external": f.blob.registered_external,
            "scan": scans[f.blob_id],
            "acquired": _acquired_out(f),
            "facts": {key: f.facts.get(key) for key in stated.FACTS},
        }
        for f in files
    ]


def _acquired_out(f: File) -> dict[str, Any] | None:
    if f.acquired_on is None:
        return None
    how = (
        "as given when it was added"
        if f.acquired_from == services.GIVEN
        else f"from {acquired.SOURCES.get(f.acquired_from, f.acquired_from)}"
    )
    return {
        "on": f.acquired_on,
        "at": f.acquired_at,
        "source": f.acquired_from,
        "source_label": how,
    }


def _job_statuses(
    user: User, jobs: Sequence[Upload | Registration], *, created_in: Project | None = None
) -> list[dict[str, Any]]:
    """The maker keeps the status. Details follow the file, or its project until it exists."""
    files = services.readable(user).filter(pk__in={job.file_id for job in jobs if job.file_id})
    file_rows = _file_out(user, list(files.select_related("blob", "created_by", "agent")))
    by_file = {f["id"]: f for f in file_rows}
    before_file = {job.project_id for job in jobs if job.file_id is None}
    readable_projects = set(
        access.readable_projects(user).filter(pk__in=before_file).values_list("pk", flat=True)
    )
    # Starting a job already authorized adding the file in this project on this request.
    # Polls and history always use current read access, including access through a record.
    rows = []
    for job in jobs:
        file = by_file.get(job.file_id)
        row = {
            "id": job.pk,
            "state": job.state,
            "created_at": job.created_at,
            "file": file,
            "error": "",
        }
        if isinstance(job, Upload):
            row["expires_at"] = job.expires_at
        just_created = created_in is not None and job.project_id == created_in.pk
        if (
            just_created
            or file is not None
            or (job.file_id is None and job.project_id in readable_projects)
        ):
            row.update(
                project=job.project_id if just_created or file is None or file["project"] else None,
                name=job.name,
                media_type=job.media_type,
                size=job.size,
                error=job.error,
            )
            if isinstance(job, Upload):
                row.update(
                    sha256=job.sha256,
                    part_size=job.part_size,
                    part_count=storage.part_count(job.size, job.part_size),
                )
            else:
                row.update(uri=job.storage_uri, version=job.object_version)
        rows.append(row)
    return rows


def _invalid(error: Refused) -> Exception:
    """A refusal about one field, as a 422 that points at it; others answer as refusals do."""
    if error.field is None:
        return error
    return Problem(
        422,
        "Some fields are invalid.",
        errors=[{"pointer": f"#/{error.field}", "detail": str(error)}],
    )


def _upload(user: User, upload_id: UUID) -> Upload:
    """The caller's own upload of a file. Nobody else's is found, nor a run's artifacts and
    checkpoints, made in its launcher's name: only the run's worker sends their parts."""
    found = Upload.objects.filter(pk=upload_id, created_by=user, kind=UploadKind.FILE).first()
    if found is None:
        raise Problem(404, "No such upload of yours.")
    return found


def _file(user: User, file_id: UUID) -> File:
    found = (
        services.readable(user)
        .select_related("blob", "created_by", "agent")
        .filter(pk=file_id)
        .first()
    )
    if found is None:
        raise Problem(404, "No such file, or you can’t read it.")
    return found


# --- Quarantine ----------------------------------------------------------------------------------


#: Places a list shows for each decision; its own page shows every one.
_LISTED_COPIES = 3

_COPY_KINDS = {
    UploadKind.FILE: "file",
    UploadKind.ARTIFACT: "artifact",
    UploadKind.CHECKPOINT: "checkpoint",
    UploadKind.BUNDLE: "bundle",
}


def _quarantine_out(
    user: User, decisions: Sequence[BlobScan], *, every: bool
) -> list[dict[str, Any]]:
    """Each decision with where its bytes are: the files sharing a blob, or the run's or
    bundle's own upload. A Quarantine Admin needn't read the projects (ADR 0217): a file's
    ``file`` is given only to someone who can."""
    blobs = [d.blob_id for d in decisions if d.blob_id is not None]
    counts = dict(
        File.objects.filter(blob_id__in=blobs)
        .order_by()
        .values("blob_id")
        .annotate(n=Count("pk"))
        .values_list("blob_id", "n")
    )
    shown = File.objects.filter(blob_id__in=blobs).select_related("project", "created_by", "agent")
    if not every:
        first = Window(RowNumber(), partition_by=[F("blob_id")], order_by=F("pk").asc())
        shown = shown.annotate(nth=first).filter(nth__lte=_LISTED_COPIES)
    files = list(shown.order_by("pk"))
    readable = set(
        services.readable(user).filter(pk__in=[f.pk for f in files]).values_list("pk", flat=True)
    )
    by_blob: dict[UUID, list[dict[str, Any]]] = {}
    for f in files:
        by_blob.setdefault(f.blob_id, []).append(
            {
                "kind": "file",
                "file": f.pk if f.pk in readable else None,
                "name": f.name,
                "media_type": f.media_type,
                "project": f.project,
                "run": None,
                "created_by": f.created_by,
                "agent": agent_out(f.agent),
                "created_at": f.created_at,
            }
        )
    uploads = {
        u.pk: u
        for u in Upload.objects.filter(
            pk__in=[d.upload_id for d in decisions if d.upload_id is not None]
        ).select_related(
            "project", "created_by", "agent", "run_artifact__run", "run_checkpoint__run"
        )
    }
    rows = []
    for decision in decisions:
        if decision.blob_id is not None:
            copies = by_blob.get(decision.blob_id, [])
            count = counts.get(decision.blob_id, 0)
            source: Any = decision.blob
        elif decision.upload_id is not None:
            upload = uploads[decision.upload_id]
            kept = getattr(upload, "run_artifact", None) or getattr(upload, "run_checkpoint", None)
            copies = [
                {
                    "kind": _COPY_KINDS[UploadKind(upload.kind)],
                    "file": None,
                    "name": upload.name,
                    "media_type": upload.media_type,
                    "project": upload.project,
                    "run": kept.run.hrid if kept is not None else None,
                    "created_by": upload.created_by,
                    "agent": agent_out(upload.agent),
                    "created_at": upload.created_at,
                }
            ]
            count = 1
            source = upload
        else:
            # iki_scan_one_source: every decision names its blob or its upload.
            raise ValueError(f"Scan decision {decision.pk} has no source.")
        release = getattr(decision, "release", None)
        rows.append(
            {
                "id": decision.pk,
                "sha256": source.sha256,
                "size": source.size,
                "state": decision.state,
                "scanner": decision.scanner,
                "signature_version": decision.signature_version,
                "scanned_at": decision.scanned_at,
                "reason": decision.reason,
                "releasable": decision.state == "infected" and release is None,
                "release": (
                    {"by": release.actor, "at": release.released_at, "reason": release.reason}
                    if release is not None
                    else None
                ),
                "files": copies,
                "file_count": count,
            }
        )
    return rows


@router.get("/quarantine-decisions", response=Page[QuarantineOut], summary="List quarantined files")
def list_quarantine(request: HttpRequest, filters: Query[QuarantineFilters]) -> dict[str, Any]:
    """For Quarantine Admins. Newest first, across the deployment: what's held now, or with
    ``released``, the infected findings released as false positives. Each entry
    identifies the held bytes, the finding, and where the bytes are: several project files
    can share one source, and so its scan."""
    me = caller(request)
    require(me, Action.QUARANTINE_MANAGE)
    found = scan_state.quarantine(released=filters.released, state=filters.state or "", q=filters.q)
    result = page(found, filters, newest_first=True)
    items = _quarantine_out(me, result["items"], every=False)
    return {"items": items, "next_cursor": result["next_cursor"]}


@router.get(
    "/quarantine-decisions/{decision_id}",
    response=QuarantineOut,
    summary="Read a quarantined file",
)
def read_quarantine(request: HttpRequest, decision_id: UUID) -> dict[str, Any]:
    """For Quarantine Admins: a held decision, or a released one, with every place its
    bytes are."""
    me = caller(request)
    require(me, Action.QUARANTINE_MANAGE)
    return _quarantine_out(me, [_reviewed(decision_id)], every=True)[0]


def _reviewed(decision_id: UUID) -> BlobScan:
    decision = scan_state.reviewed(decision_id)
    if decision is None:
        raise Problem(404, "No such quarantined file.")
    return decision


@router.post(
    "/quarantine-decisions/{decision_id}/release",
    response=QuarantineOut,
    summary="Release an infected file after reviewing its finding",
)
def release_quarantine(
    request: HttpRequest, decision_id: UUID, payload: ReleaseQuarantineIn
) -> dict[str, Any]:
    """For Quarantine Admins. The given reason and the reviewer are recorded with the
    release and in the audit log. Every file covered by this decision becomes usable and
    keeps its original finding. An incomplete inspection can't be released."""
    me = caller(request)
    require(me, Action.QUARANTINE_MANAGE)
    scan_state.release(decision_id, actor=me, reason=payload.reason)
    return _quarantine_out(me, [_reviewed(decision_id)], every=True)[0]


# --- Uploads -------------------------------------------------------------------------------------


@router.post("/uploads", response={201: StartedOut}, summary="Start adding a file")
def start_upload(request: HttpRequest, payload: UploadIn) -> Status[dict[str, Any]]:
    """Answers with the parts to send. An empty file needs none."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    _storage()
    try:
        upload = services.start(
            me,
            target,
            name=payload.name,
            media_type=payload.media_type,
            size=payload.size,
            sha256=payload.sha256,
            acquired_at=payload.acquired_at,
        )
    except Refused as error:
        raise _invalid(error) from None
    upload.refresh_from_db()
    status = _job_statuses(me, [upload], created_in=target)[0]
    parts = services.parts(upload) if upload.state == UploadState.PENDING else []
    return Status(201, {**status, "parts": parts})


@router.get("/uploads", response=Page_UploadOut_, summary="Your uploads")
def list_uploads(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first."""
    me = caller(request)
    mine = Upload.objects.filter(created_by=me, kind=UploadKind.FILE)
    result = page(mine, params, newest_first=True)
    result["items"] = _job_statuses(me, result["items"])
    return result


@router.get(
    "/uploads/{upload_id}", response=UploadOut | UploadStatusOut, summary="One of your uploads"
)
def get_upload(request: HttpRequest, upload_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _job_statuses(me, [_upload(me, upload_id)])[0]


@router.post(
    "/uploads/{upload_id}/parts", response=list[PartOut], summary="Sign an upload's parts again"
)
def sign_parts(request: HttpRequest, upload_id: UUID, payload: PartsIn) -> list[dict[str, Any]]:
    """New URLs, when the earlier ones expired before every part was sent."""
    me = caller(request)
    _storage()
    return services.parts(_upload(me, upload_id), payload.numbers)


@router.post(
    "/uploads/{upload_id}/complete",
    response={202: UploadOut | UploadStatusOut, 200: UploadOut | UploadStatusOut},
    summary="Finish an upload",
)
def complete_upload(request: HttpRequest, upload_id: UUID) -> Status[dict[str, Any]]:
    """Once every part has arrived. Iki then checks the bytes (202), and the upload becomes
    ready after its malware scan, or fails if its bytes don't match the SHA-256.
    Finishing it again returns its current state."""
    me = caller(request)
    _storage()
    upload = services.complete(_upload(me, upload_id))
    waiting = upload.state in (UploadState.CHECKING, UploadState.SCANNING)
    return Status(202 if waiting else 200, _job_statuses(me, [upload])[0])


@router.delete("/uploads/{upload_id}", response={204: None}, summary="Cancel an upload")
def cancel_upload(request: HttpRequest, upload_id: UUID) -> Status[None]:
    """What of it arrived is deleted. A finished upload stays as it is."""
    me = caller(request)
    _storage()
    services.cancel(_upload(me, upload_id))
    return Status(204, None)


# --- Registering in place ------------------------------------------------------------------------


@router.get(
    "/registrations/locations",
    response=Page[RegistrationLocationOut],
    summary="Where files can be registered",
)
def registration_locations(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """The approved buckets and prefixes, set by your administrators, by URI."""
    caller(request)
    return page_sorted(
        ({"uri": uri} for uri in services.locations()),
        params,
        by=("uri",),
        key=lambda u: (u["uri"],),
    )


@router.post(
    "/registrations",
    response={202: RegistrationOut, 201: RegistrationOut},
    summary="Register a file where it is",
)
def register_file(request: HttpRequest, payload: RegistrationIn) -> Status[dict[str, Any]]:
    """Pins one version of an object in an approved, versioned bucket. Iki reads it to learn
    its SHA-256 and scan it (202). A version with a completed registration can be reused
    immediately (201). Its file says whether it is usable."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    _storage()
    try:
        registration = services.register(
            me,
            target,
            uri=payload.uri,
            version=payload.version,
            name=payload.name,
            media_type=payload.media_type,
            acquired_at=payload.acquired_at,
        )
    except Refused as error:
        raise _invalid(error) from None
    registration.refresh_from_db()
    ready = registration.state == RegistrationState.READY
    return Status(201 if ready else 202, _job_statuses(me, [registration], created_in=target)[0])


@router.get(
    "/registrations",
    response=Page_RegistrationOut_,
    summary="Your registrations",
)
def list_registrations(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first."""
    me = caller(request)
    result = page(Registration.objects.filter(created_by=me), params, newest_first=True)
    result["items"] = _job_statuses(me, result["items"])
    return result


@router.get(
    "/registrations/{registration_id}",
    response=RegistrationOut | RegistrationStatusOut,
    summary="One of your registrations",
)
def get_registration(request: HttpRequest, registration_id: UUID) -> dict[str, Any]:
    me = caller(request)
    found = Registration.objects.filter(pk=registration_id, created_by=me).first()
    if found is None:
        raise Problem(404, "No such registration of yours.")
    return _job_statuses(me, [found])[0]


# --- Files ---------------------------------------------------------------------------------------


def _filters(me: User, params: FileFilters | FacetParams) -> listing.Filters:
    from iki_server.runs.models import ActionRun
    from iki_server.runs.services import visible_runs

    sample = None
    if params.sample:
        sample = _readable_entity(me, params.sample)
        if sample is None:
            raise _unknown("sample", "No such sample, or you can’t read it.")
    run: ActionRun | None = None
    if params.run:
        runs = visible_runs(me)
        try:
            run = runs.filter(pk=UUID(params.run)).first()
        except ValueError:
            run = runs.filter(hrid=params.run.upper()).first()
        if run is None:
            raise _unknown("run", "No such run, or you can’t read it.")
    return listing.Filters(
        q=params.q,
        formats=tuple(_media_types(params.format, "format")),
        acquired_from=params.acquired_from,
        acquired_to=params.acquired_to,
        sample=sample,
        run=run,
        accepts=tuple(_media_types(params.accepts, "accepts")),
        max_size=params.max_size,
    )


def _media_types(given: list[str], parameter: str) -> list[str]:
    out = []
    for each in given:
        text = each.strip().lower()
        if text in registry.file_formats().formats or (
            text.endswith("/*") and services.MEDIA_KIND.fullmatch(text[:-2])
        ):
            out.append(text)
            continue
        try:
            out.append(services.clean_media_type(text))
        except Refused:
            raise _unknown(
                parameter, "Give an installed format ID, media type or family such as image/*."
            ) from None
    return out


def _unknown(parameter: str, detail: str) -> Problem:
    return Problem(
        422, "Some fields are invalid.", errors=[{"parameter": parameter, "detail": detail}]
    )


def _readable_entity(me: User, ref: str) -> Entity | None:
    readable = access.visible(me)
    try:
        return readable.filter(pk=UUID(ref)).first()
    except ValueError:
        return readable.filter(hrid=ref.strip().upper()).order_by("created_at", "pk").first()


def _scope(me: User, project_id: UUID | None) -> Any:  # noqa: ANN401 - a Project or None
    return project(me, project_id, Action.PROJECT_READ) if project_id else None


def _rows_out(me: User, files: list[File]) -> list[dict[str, Any]]:
    counts = listing.used_by_counts(me, files)
    return [
        {**out, "used_by": {"records": counts[f.pk][0], "runs": counts[f.pk][1]}}
        for f, out in zip(files, _file_out(me, files), strict=True)
    ]


@router.get("/files", response=FilePageOut, summary="Files you can read")
def list_files(request: HttpRequest, filters: Query[FileFilters]) -> dict[str, Any]:
    """A project's files, or every file you can read: those added to the projects, and
    those their records hold (ADR 0177). Found by part of the name or of what the file
    states (ADR 0224), format, when acquired, sample and run; newest first unless ``sort``
    says otherwise. Each says how many records and runs you can read use it."""
    me = caller(request)
    scope = _scope(me, filters.project)
    try:
        files, next_cursor = listing.page(
            me,
            scope,
            _filters(me, filters),
            sort=filters.sort,
            cursor=filters.cursor,
            limit=filters.limit,
        )
    except listing.CursorError:
        raise _unknown("cursor", "Not a cursor from this list, sorted so.") from None
    return {"items": _rows_out(me, files), "next_cursor": next_cursor}


@router.get("/files/count", response=FileCountOut, summary="How many files match")
def count_files(request: HttpRequest, params: Query[FacetParams]) -> dict[str, Any]:
    """As ``GET /files`` would list them, up to 1,000."""
    me = caller(request)
    filters = _filters(me, params)
    total, more = listing.count(me, _scope(me, params.project), filters)
    return {"total": total, "more_than_total": more}


class FormatFacetParams(FacetParams, PageParams):
    pass


@router.get("/files/formats", response=Page[FormatOut], summary="The formats of files")
def file_formats(request: HttpRequest, params: Query[FormatFacetParams]) -> dict[str, Any]:
    """Format labels and counts for readable files matching the other filters, the most
    common first. Aliases count together. A file of an undeclared type keeps its media type
    as its label."""
    me = caller(request)
    counted = listing.formats(me, _scope(me, params.project), _filters(me, params))
    # Counted over every matching file in one query, so paged as counted.
    found = page_sorted(counted, params, by=("-count", "media_type"), key=lambda f: (-f[1], f[0]))
    catalog = registry.file_formats()
    out = []
    for media_type, count in found["items"]:
        fmt = catalog.by_media_type(media_type)
        out.append(
            {
                "id": fmt.id if fmt else None,
                "label": fmt.label if fmt else media_type,
                "media_type": media_type,
                "count": count,
            }
        )
    return {**found, "items": out}


@router.get("/formats", response=Page[InstalledFormatOut], summary="The installed file formats")
def installed_formats(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Core and plugin formats, in recognition order for shared filename extensions."""
    caller(request)
    catalog = registry.file_formats()
    found = page_sorted(
        enumerate(catalog.formats.values()),
        params,
        by=("order", "id"),
        key=lambda f: (f[0], f[1].id),
    )
    items = [
        {
            "id": fmt.id,
            "label": fmt.label,
            "canonical": fmt.canonical,
            "media_types": fmt.media_types,
            "extensions": fmt.extensions,
            "signatures": fmt.signatures,
            "reader": fmt.reader,
            "viewer": fmt.viewer,
            "parsers": catalog.parsers(fmt.id),
        }
        for _, fmt in found["items"]
    ]
    return {**found, "items": items}


def _run_ref(run: Any) -> dict[str, Any]:  # noqa: ANN401 - an ActionRun
    from iki_server.runs import catalog

    action = catalog.action(run.action)
    return {
        "id": run.pk,
        "hrid": run.hrid,
        "action": run.action,
        "label": (action.label or action.name) if action is not None else run.action,
    }


def _origin(me: User, f: File) -> dict[str, Any]:
    from iki_server.runs.models import RunUpload
    from iki_server.runs.services import visible_runs

    out: dict[str, Any] = {"kind": "import", "run": None, "uri": None, "version": None}
    upload = Upload.objects.filter(file=f).first()
    if upload is not None:
        made = RunUpload.objects.filter(upload=upload).values_list("run_id", flat=True).first()
        if made is None:
            return {**out, "kind": "upload"}
        run = visible_runs(me).filter(pk=made).first()
        return {**out, "kind": "run", "run": _run_ref(run) if run is not None else None}
    if Registration.objects.filter(file=f).exists():
        return {
            **out,
            "kind": "registration",
            "uri": f.blob.storage_uri,
            "version": f.blob.object_version,
        }
    return out


def _titles(entities: list[Entity]) -> dict[Any, str]:
    from iki_server.entities import registry
    from iki_spec import titles

    values = entity_services.current_values_of(entities)
    types = registry.types()
    out = {}
    for e in entities:
        fields = shape(e.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values.get(e.pk, {}).items() if n in fields}
        entity_type = types.get(e.type)
        template = registry.display_title(entity_type) if entity_type is not None else None
        out[e.pk] = titles.render(template, shown, e.hrid, fields)
    return out


@router.get("/files/{file_id}", response=FileDetailOut, summary="A file")
def get_file(request: HttpRequest, file_id: UUID) -> dict[str, Any]:
    """With where it came from, the samples it belongs to, and how many records and runs
    use it, of those you can read."""
    me = caller(request)
    found = _file(me, file_id)
    samples = listing.samples_of(me, found)
    titles = _titles([e for e, _ in samples])
    return {
        **_rows_out(me, [found])[0],
        "read_groups": found.read_groups,
        "origin": _origin(me, found),
        "samples": [
            {
                "id": e.pk,
                "hrid": e.hrid,
                "title": titles[e.pk],
                "type": e.type,
                "via_run": _run_ref(run) if run is not None else None,
            }
            for e, run in samples
        ],
    }


@router.get(
    "/files/{file_id}/records",
    response=Page[HoldingRecordOut],
    summary="The records that hold a file",
)
def file_records(request: HttpRequest, file_id: UUID, params: Query[PageParams]) -> dict[str, Any]:
    """Every record you can read that holds the file in a file field, at any revision,
    with the revisions that hold it (ADR 0177). Oldest first."""
    me = caller(request)
    found = _file(me, file_id)
    result = page(listing.holding_records(me, found), params)
    records: list[Entity] = result["items"]
    titles = _titles(records)
    result["items"] = [
        {
            "id": e.pk,
            "hrid": e.hrid,
            "title": titles[e.pk],
            "type": e.type,
            "revision": e.revision,
            "spans": [
                {"first": s.first, "last": s.last, "fields": list(s.fields)}
                for s in listing.held_spans(e, found.pk)
            ],
        }
        for e in records
    ]
    return result


@router.get(
    "/files/{file_id}/runs", response=Page[UsingRunOut], summary="The runs that used a file"
)
def file_runs(request: HttpRequest, file_id: UUID, params: Query[PageParams]) -> dict[str, Any]:
    """Every run you can read that read the file (as a param, or an input's) or made it,
    newest first."""
    from iki_server.runs.models import RunFile

    me = caller(request)
    found = _file(me, file_id)
    result = page(
        listing.using_runs(me, found).select_related("project"), params, newest_first=True
    )
    runs = result["items"]
    roles: dict[Any, list[str]] = {}
    for run_id, role in RunFile.objects.filter(
        file=found, run__in=[r.pk for r in runs]
    ).values_list("run_id", "role"):
        roles.setdefault(run_id, []).append(role)
    order = {"param": 0, "input": 1, "made": 2}
    result["items"] = [
        {
            **_run_ref(r),
            "status": r.status,
            "project": r.project,
            "roles": sorted(roles.get(r.pk, []), key=order.__getitem__),
            "created_at": r.created_at,
        }
        for r in runs
    ]
    return result


class ContentUrlOut(Schema):
    url: str = Field(description="A signed URL that reads the file, for five minutes.")
    expires_at: datetime
    sha256: str = Field(description="What the bytes it reads hash to: check them against it.")


def _download_link(me: User, file_id: UUID) -> ContentUrlOut:
    found = _file(me, file_id)
    require_usable(found.blob)
    _storage()
    blob = found.blob
    expires_at = timezone.now().replace(microsecond=0) + timedelta(
        seconds=storage.DOWNLOAD_URL_SECONDS
    )
    url = storage.download_url(blob.storage_uri, blob.object_version, found.name, found.media_type)
    audit.record(
        "file.download_link_issued",
        actor=me,
        target=found,
        project=found.project,
        data={"sha256": blob.sha256, "expires_at": expires_at.isoformat()},
    )
    return ContentUrlOut(url=url, expires_at=expires_at, sha256=blob.sha256)


@router.get("/files/{file_id}/content", response={302: None}, summary="Download a file")
def download_file(request: HttpRequest, file_id: UUID) -> HttpResponse:
    """Redirects to a signed URL that works for five minutes. It downloads the file under
    its name, rather than opening it in the browser."""
    link = _download_link(caller(request), file_id)
    response = HttpResponse(status=302)
    response["Location"] = link.url
    response["Cache-Control"] = "no-store"
    return response


@router.get("/files/{file_id}/content-url", response=ContentUrlOut, summary="A link to read a file")
def file_content_url(request: HttpRequest, file_id: UUID, response: HttpResponse) -> ContentUrlOut:
    """For pages that read a file themselves, such as the structure viewer: the store
    lets Iki's pages read it from the link (CORS), which ``/content``'s redirect can't
    offer, since a redirect to another origin hides who's asking."""
    response["Cache-Control"] = "no-store"
    return _download_link(caller(request), file_id)
