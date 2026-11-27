"""Many records as one file (ADR 0229): ``/entity-exports``."""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema
from ninja.responses import Status

from iki_server.api.common import caller, project
from iki_server.api.entities import Condition, SortIn, _searching
from iki_server.api.files import ContentUrlOut, _storage
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.entities import record_exports as exporting
from iki_server.entities import registry, search
from iki_server.entities.models import RecordExport
from iki_server.files import storage
from iki_server.org.services import Refused

router = Router(tags=["entities"])

#: The formats many records make one file of: Iki's (genbank, fasta, protein-fasta, sdf),
#: or a plugin's writer's, by its qualified ID (ADR 0230).
ManyFormat = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9._/-]*$")]


class ExportSearchIn(Schema):
    """The records a table shows, as ``POST /entities/search`` finds them."""

    type: str | None = Field(None, description="This type and its subtypes.")
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(None, max_length=200)
    collection: str | None = Field(None, max_length=100)
    run: str | None = Field(None, max_length=100)
    where: Condition | None = None
    sort: SortIn | None = None


class RecordExportIn(Schema):
    format: ManyFormat = Field(
        description="A format every record's type exports, as its `exports` list them, of "
        "which many records make one file (`many: file`), or a ZIP of one file each (`many: "
        "zip`)."
    )
    entities: list[str] = Field(
        default_factory=list,
        max_length=exporting.MAX_RECORDS,
        description="The records, by UUID or HRID, in the file's order. Or give `search`.",
    )
    search: ExportSearchIn | None = Field(
        None,
        description="Every record this search finds, in its order, up to "
        f"{exporting.MAX_RECORDS:,}: a table's whole result.",
    )
    project: UUID | None = Field(
        None,
        description="For an HRID naming a registry entry's record in several projects: this "
        "project's (ADR 0115).",
    )
    fields: list[str] | None = Field(
        None,
        max_length=500,
        description="An SDF's data: the fields it carries, in order. Every one all its "
        "records' types can carry, if left out.",
    )


class RecordExportOut(Schema):
    id: UUID
    state: Literal["queued", "writing", "ready", "failed", "expired"]
    format: str
    selection: str = Field(description="What was picked, in words.")
    count: int = Field(description="How many records it holds.")
    done: int = Field(description="How many of them are written so far.")
    file_name: str = Field(alias="name")
    size: int | None = Field(description="The file's size in bytes, once it's ready.")
    sha256: str = Field(description="The file's SHA-256, once it's ready.")
    fields: list[str] = Field(description="The fields an SDF carries as data.")
    notes: dict[str, list[str]] = Field(
        description="What the file couldn't hold of each record, by HRID, as the file says "
        "it where the format has a place: GenBank's COMMENT, an SDF's IKI_NOTES. FASTA has "
        "none, so its notes are only here."
    )
    refused: dict[str, str] = Field(
        description="Records that couldn't be written, by HRID, and why: the export failed, "
        "so no file holds fewer records than were asked for."
    )
    error: str = Field(description="Why it failed, if it did.")
    writer: dict[str, str] = Field(
        description="Which code wrote it: Iki's `builtin` and `version`; or a plugin's "
        "writer's `plugin`, `writer`, `commit`, `module_sha256` and `abi` (ADR 0230)."
    )
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None = Field(
        description="When its file is deleted. Files of records in a project under a legal "
        "hold are kept until the hold is released."
    )

    @staticmethod
    def resolve_count(made: RecordExport) -> int:
        return len(made.records)


def _refused(error: Refused) -> Problem:
    """A 422 that says why in its detail, since what's picked or searched isn't a field a
    form shows; it points at the part of the request that's why, too."""
    where = [{"pointer": f"#/{error.field}", "detail": str(error)}] if error.field else None
    return Problem(422, str(error), errors=where)


def _mine(request: HttpRequest, export_id: UUID) -> RecordExport:
    made = exporting.theirs(caller(request)).filter(pk=export_id).first()
    if made is None:
        raise Problem(404, "No such export, or it isn't yours.")
    return made


def _described(wanted: search.Search, place: Any) -> str:  # noqa: ANN401 - a Project
    """``All proteins in Kinase Atlas matching the table's search``."""
    entity_type = registry.types().get(wanted.type) if wanted.type else None
    what = (
        (entity_type.label_plural or f"{entity_type.label or entity_type.name}s").lower()
        if entity_type is not None
        else "records"
    )
    where = f" in {place.name}" if place is not None else ""
    narrowed = wanted.q or wanted.where or wanted.collection or wanted.run
    return f"All {what}{where}" + (" matching the table's search" if narrowed else "")


@router.post(
    "/entity-exports",
    response={201: RecordExportOut, 202: RecordExportOut},
    summary="Export records as one file, such as FASTA or SDF",
)
def start_record_export(request: HttpRequest, payload: RecordExportIn) -> Status[Any]:
    """Records you can read as one file of a format each of their types exports (ADR 0229):
    those picked, in order, or every one a search finds, up to 10,000. Each is written at
    the revision it has now, read in one snapshot, so editing one afterwards doesn't change
    the file. Up to 100 are written at once (201, ready to download); more in the
    background (202): read the export until it's ready. A record the format can't be
    written for fails it, naming each such record, so no file holds fewer than asked for.
    Asking and each download are in the audit log."""
    me = caller(request)
    _storage()
    place = project(me, payload.project) if payload.project else None
    wanted = None
    described = ""
    if payload.search is not None:
        given = payload.search
        searched_in = project(me, given.project) if given.project else None
        wanted = search.Search(
            type=given.type,
            exact_type=given.exact_type,
            project=given.project,
            archived=given.archived,
            q=given.q,
            collection=given.collection,
            run=given.run,
            where=given.where.model_dump(by_alias=True, exclude_unset=True)
            if given.where
            else None,
            sort=given.sort.field if given.sort else None,
            descending=given.sort.descending if given.sort else False,
        )
        described = _described(wanted, searched_in)
    try:
        with _searching():
            made = exporting.start(
                me,
                payload.format,
                entities=payload.entities,
                wanted=wanted,
                project=place,
                data=payload.fields,
                described=described,
            )
    except exporting.NotReadable as error:
        raise Problem(404, str(error), code="not_found") from None
    except Refused as error:
        raise _refused(error) from None
    return Status(201 if made.state != "queued" else 202, made)


@router.get("/entity-exports", response=Page[RecordExportOut], summary="Your exports of records")
def list_record_exports(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first: each file of records you asked for, ready to download again until it
    expires a week after it's written."""
    found = exporting.theirs(caller(request))
    return page(found, params, newest_first=True)


@router.get("/entity-exports/{export_id}", response=RecordExportOut, summary="An export of records")
def get_record_export(request: HttpRequest, export_id: UUID) -> RecordExport:
    """How far it's got (``done`` of ``count``), and once it's ready, its file."""
    return _mine(request, export_id)


@router.get(
    "/entity-exports/{export_id}/content",
    response={302: None},
    summary="Download an export of records",
)
def download_record_export(request: HttpRequest, export_id: UUID) -> HttpResponse:
    """Redirects to the file, for a few minutes, while you can still read every record it
    holds and what they refer to. Each download is in the audit log."""
    response = HttpResponse(status=302)
    response["Location"] = _link(request, export_id).url
    response["Cache-Control"] = "no-store"
    return response


@router.get(
    "/entity-exports/{export_id}/content-url",
    response=ContentUrlOut,
    summary="A link to an export of records",
)
def record_export_content_url(
    request: HttpRequest, export_id: UUID, response: HttpResponse
) -> ContentUrlOut:
    """The signed URL ``/content`` redirects to, for a client that fetches it without its
    Iki token, such as an agent. Each one issued is in the audit log, as a download."""
    response["Cache-Control"] = "no-store"
    return _link(request, export_id)


def _link(request: HttpRequest, export_id: UUID) -> ContentUrlOut:
    made = _mine(request, export_id)
    _storage()
    expires_at = timezone.now().replace(microsecond=0) + timedelta(
        seconds=storage.DOWNLOAD_URL_SECONDS
    )
    try:
        url = exporting.download_url(caller(request), made)
    except Refused as error:
        raise Problem(409, str(error), code="refused") from None
    return ContentUrlOut(url=url, expires_at=expires_at, sha256=made.sha256)
