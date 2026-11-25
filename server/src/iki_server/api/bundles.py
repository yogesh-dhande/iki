"""Bundles: a project's entities, with their history, for another deployment (ADR 0096)."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema
from ninja.responses import Status
from pydantic import field_serializer

from iki_server.api.common import caller, project
from iki_server.api.files import PartOut, PartsIn, _invalid, _storage
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.bundles import applying, exports, imports
from iki_server.bundles.models import (
    BundleExport,
    BundleImport,
    ImportEntity,
    ImportPerson,
    ImportState,
)
from iki_server.entities import access
from iki_server.files import services as files
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused

router = Router(tags=["bundles"])


class ExportIn(Schema):
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Why: the bundle, the export and the audit log keep it.",
    )
    collection: str | None = Field(
        None,
        description="A collection in the project, by ID or HRID. A static one's members go "
        "with it; a dynamic one's, what its search finds for you now in this project.",
    )
    entities: list[str] = Field(
        default_factory=list,
        max_length=exports.MAX_PICKED,
        description="Or entities owned by the project, by ID or HRID.",
    )


class ExportCounts(Schema):
    entities: int
    revisions: int
    files: int
    outside: int = Field(description="Entities its values or runs refer to that aren't in it.")
    runs: int = Field(0, description="The runs that made or changed its entities.")


class ExportOut(Schema):
    id: UUID
    project: UUID = Field(alias="project_id")
    state: Literal["queued", "writing", "ready", "failed", "expired"]
    requested_by: PersonRef
    reason: str
    selection: str = Field(description="What was picked, in words.")
    error: str = Field(description="Why it failed, if it did.")
    sha256: str = Field(description="The archive's SHA-256, once it's ready.")
    size: int | None = Field(description="The archive's size in bytes, once it's ready.")
    counts: ExportCounts | None
    file_name: str
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None = Field(
        description="When its archive is deleted. Exports from a project under a legal hold "
        "are kept until the hold is released."
    )

    @staticmethod
    def resolve_selection(made: BundleExport, context: dict[str, Any]) -> str:
        if access.all_references_readable(caller(context["request"]), made.picked):
            return made.selection
        return "Selected records"

    @staticmethod
    def resolve_error(made: BundleExport, context: dict[str, Any]) -> str:
        if made.error and not access.all_references_readable(
            caller(context["request"]), made.picked
        ):
            return "The selected records could not be exported."
        return made.error

    @staticmethod
    def resolve_counts(made: BundleExport) -> dict[str, int] | None:
        return made.counts or None

    @staticmethod
    def resolve_file_name(made: BundleExport) -> str:
        return exports.file_name(made)


def _export(request: HttpRequest, export_id: UUID) -> BundleExport:
    me = caller(request)
    made = (
        BundleExport.objects.select_related("project__team", "requested_by")
        .filter(pk=export_id)
        .first()
    )
    if made is None or not can(me, Action.PROJECT_EXPORT, made.project):
        raise Problem(404, "No such export, or it isn't yours to see.")
    return made


@router.post("/projects/{project_id}/bundles", response={202: ExportOut}, summary="Export a bundle")
def start_export(request: HttpRequest, project_id: UUID, payload: ExportIn) -> Status[Any]:
    """Asks for a bundle of a collection or of entities, with every revision, the files they
    name and who made them, and what they refer to in the project (ADR 0096). It's written in
    the background (202): read the export until it's ready, then download it. Needs the
    project's Admin role, and a token's admin scope."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_EXPORT)
    _storage()
    try:
        made = exports.start(
            me,
            target,
            reason=payload.reason,
            collection=payload.collection,
            entities=payload.entities,
        )
    except Refused as error:
        raise _invalid(error) from None
    return Status(202, BundleExport.objects.select_related("requested_by").get(pk=made.pk))


@router.get(
    "/projects/{project_id}/bundles", response=Page[ExportOut], summary="The project's exports"
)
def list_exports(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Newest first: who asked for each, why, and whether it's ready."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_EXPORT)
    found = BundleExport.objects.filter(project=target).select_related("project", "requested_by")
    return page(found, params, newest_first=True)


@router.get("/bundles/{export_id}", response=ExportOut, summary="An export")
def get_export(request: HttpRequest, export_id: UUID) -> BundleExport:
    return _export(request, export_id)


@router.get("/bundles/{export_id}/content", response={302: None}, summary="Download an export")
def download_export(request: HttpRequest, export_id: UUID) -> HttpResponse:
    """Redirects to the archive, for a few minutes. Each download is in the audit log."""
    made = _export(request, export_id)
    _storage()
    try:
        url = exports.download_url(caller(request), made)
    except Refused as error:
        raise Problem(409, str(error), code="refused") from None
    response = HttpResponse(status=302)
    response["Location"] = url
    return response


# --- Imports ----------------------------------------------------------------------------------


class ImportIn(Schema):
    name: str = Field(max_length=1000, description="The archive's file name.")
    size: int = Field(gt=0, description="The archive's size in bytes.")
    sha256: str = Field(
        min_length=64, max_length=64, description="Of the archive's bytes, as hex: Iki checks them."
    )


class ImportSource(Schema):
    deployment: str = Field(description="The deployment it's from, as its signature says.")
    url: str
    iki: str = Field(description="The Iki release that made it.")
    exported_at: datetime
    exported_by: dict[str, str] = Field(description="Their name and email address.")
    reason: str
    project: dict[str, str] = Field(description="Its id, team, name and slug there.")
    selection: str


class ImportCounts(Schema):
    entities: int
    new: int
    update: int = Field(description="Here from an earlier import: their newer revisions are added.")
    skip: int = Field(description="Here already: nothing changes.")
    conflict: int
    renumbered: int = Field(description="New ones whose HRID is another entity's here.")
    revisions: int = Field(description="The revisions importing adds.")
    people: int
    placeholders: int = Field(description="People nobody here matches by email.")
    files: int
    runs: int = Field(0, description="The runs that made or changed them, new here.")
    runs_here: int = Field(0, description="Runs here already, from an earlier import.")
    runs_renumbered: int = Field(0, description="New runs whose HRID is another run's here.")


class ImportType(Schema):
    type: str
    plugin: str
    schema_version: int = Field(description="Its schema version in the bundle.")
    here: int | None = Field(description="Its schema version here; none if it isn't here.")


class ImportFiles(Schema):
    files: int
    here: int = Field(description="Here already.")
    carried: int = Field(description="The distinct contents the bundle carries.")
    registered: int = Field(description="Registered where they are (ADR 0055), not carried.")


class OutsideRef(Schema):
    id: UUID
    hrid: str | None = Field(description="Given when the exporter could read it.")
    type: str | None
    why: Literal["another-project", "history", "purged", "run", "unreadable"]
    here: bool = Field(description="Whether an entity with its ID is here.")


class ImportOutside(Schema):
    count: int = Field(description="The entities its values refer to that aren't in it.")
    here: int = Field(description="How many of them are here.")
    items: list[OutsideRef] = Field(description=f"The first {imports.OUTSIDE_SHOWN}.")


class ImportPlan(Schema):
    counts: ImportCounts
    types: list[ImportType]
    files: ImportFiles
    outside: ImportOutside
    problems: list[str] = Field(description="What keeps it from being applied.")
    applicable: bool = Field(description="Whether it can be applied as planned.")


class ImportOut(Schema):
    id: UUID
    project: UUID = Field(alias="project_id")
    state: Literal[
        "uploading", "planning", "planned", "refused", "applying", "applied", "failed", "discarded"
    ]
    error: str = Field(description="Why it was refused or failed, if it was.")
    requested_by: PersonRef
    name: str = Field(description="The archive's file name, as uploaded.")
    sha256: str
    size: int
    upload: UUID = Field(alias="upload_id", description="Its archive's upload.")
    source: ImportSource | None = Field(description="Where it's from, once it's been read.")
    trusted_as: str = Field(description="The trusted deployment its signature is from.")
    plan: ImportPlan | None = Field(
        description="Once planned: counts, types, files, what it refers to outside it, and "
        "the problems that keep it from being applied (empty when it can be)."
    )
    created_at: datetime
    planned_at: datetime | None = Field(
        description="Revision of the published plan. Pass this value unchanged when applying it."
    )
    applied_by: PersonRef | None = Field(description="Who confirmed applying it.")
    reason: str = Field(description="Why they applied it.")
    applied_at: datetime | None

    @field_serializer(
        "planned_at",
        return_type=Annotated[str, Field(json_schema_extra={"format": "date-time"})] | None,
    )
    def serialize_planned_at(self, value: datetime | None) -> str | None:
        # DjangoJSONEncoder truncates datetimes to milliseconds. The revision must round-trip.
        return value.isoformat() if value is not None else None

    @staticmethod
    def resolve_state(made: BundleImport) -> str:
        return imports.state_of(made)[0]

    @staticmethod
    def resolve_error(made: BundleImport) -> str:
        return imports.state_of(made)[1]

    @staticmethod
    def resolve_name(made: BundleImport) -> str:
        return made.upload.name

    @staticmethod
    def resolve_source(made: BundleImport) -> dict[str, Any] | None:
        return made.source or None

    @staticmethod
    def resolve_plan(made: BundleImport) -> dict[str, Any] | None:
        return made.plan or None


class ImportStartedOut(ImportOut):
    parts: list[PartOut] = Field(description="Every part of the archive, with where to send it.")

    @staticmethod
    def resolve_parts(made: BundleImport) -> list[dict[str, Any]]:
        return files.parts(made.upload)


class ImportEntityOut(Schema):
    id: UUID = Field(alias="entity_id", description="Its ID, which importing keeps.")
    type: str
    hrid: str = Field(description="Its HRID where it's from.")
    hrid_here: str = Field(description="Its HRID here, or empty when it gets a new one.")
    title: str
    action: Literal["new", "update", "skip", "conflict"]
    revisions: int = Field(description="How many it has in the bundle.")
    adds: int = Field(description="How many of them importing adds.")
    why: str


class ImportPersonOut(Schema):
    id: UUID = Field(alias="source_id", description="Their ID where the bundle is from.")
    kind: Literal["person", "service"]
    name: str
    email: str
    user: PersonRef | None = Field(
        description="Who they are here: someone matched by email, or, once it's applied, "
        "their placeholder."
    )
    placeholder: bool = Field(
        description="Nobody here is them: a placeholder who can't sign in stands for them, "
        "until someone with their email does."
    )

    @staticmethod
    def resolve_placeholder(person: ImportPerson) -> bool:
        return person.user is None or person.user.is_placeholder


class ApplyIn(Schema):
    planned_at: datetime = Field(description="The planned_at of the plan being confirmed.")
    reason: str = Field(
        min_length=1, max_length=2000, description="Why: the import and the audit log keep it."
    )


class ImportMemberParams(PageParams):
    planned_at: datetime | None = Field(
        None, description="Read this revision of the plan. A newer plan answers 409."
    )


class ImportEntityParams(ImportMemberParams):
    action: Literal["new", "update", "skip", "conflict"] | None = None


def _import(request: HttpRequest, import_id: UUID) -> BundleImport:
    me = caller(request)
    made = (
        BundleImport.objects.select_related("project__team", "requested_by", "applied_by", "upload")
        .filter(pk=import_id)
        .first()
    )
    if made is None or not can(me, Action.PROJECT_IMPORT, made.project):
        raise Problem(404, "No such import, or it isn't yours to see.")
    return made


def _member_page[M: Model](
    made: BundleImport, members: QuerySet[M], params: ImportMemberParams
) -> dict[str, Any]:
    result = page(members, params)
    # The page is materialized in one query. Fence it with the import read before it and a
    # fresh check afterward, so a replan or an apply cannot mix versions during pagination.
    if params.planned_at is not None and (
        made.planned_at != params.planned_at
        or made.state == ImportState.DISCARDED
        or not BundleImport.objects.filter(
            pk=made.pk, planned_at=params.planned_at, state=made.state
        ).exists()
    ):
        raise Problem(409, "The plan changed. Review the current plan.", code="stale_plan")
    return result


def _imported(made: BundleImport) -> BundleImport:
    return BundleImport.objects.select_related("requested_by", "applied_by", "upload").get(
        pk=made.pk
    )


@router.post(
    "/projects/{project_id}/imports",
    response={201: ImportStartedOut},
    summary="Start importing a bundle",
)
def start_import(request: HttpRequest, project_id: UUID, payload: ImportIn) -> Status[Any]:
    """Answers with the parts of the archive to send, as an upload does. Once they're in,
    complete it: Iki checks the bundle and plans the import, changing nothing (ADR 0096).
    Needs an Org Admin who is also the project's Admin, and a token's admin scope."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_IMPORT)
    _storage()
    try:
        made = imports.start(
            me, target, name=payload.name, size=payload.size, sha256=payload.sha256
        )
    except Refused as error:
        raise _invalid(error) from None
    return Status(201, _imported(made))


@router.post(
    "/imports/{import_id}/parts", response=list[PartOut], summary="Sign an import's parts again"
)
def sign_import_parts(
    request: HttpRequest, import_id: UUID, payload: PartsIn
) -> list[dict[str, Any]]:
    """New URLs, when the earlier ones expired before every part was sent."""
    made = _import(request, import_id)
    _storage()
    return files.parts(made.upload, payload.numbers)


@router.post(
    "/imports/{import_id}/complete",
    response={202: ImportOut, 200: ImportOut},
    summary="Finish uploading an import",
)
def complete_import(request: HttpRequest, import_id: UUID) -> Status[BundleImport]:
    """Once every part has arrived. Iki checks the bytes, then the bundle, and plans it
    (202). Finishing it again changes nothing."""
    made = _import(request, import_id)
    _storage()
    files.complete(made.upload)
    made = _imported(made)
    return Status(200 if made.state != ImportState.UPLOADING else 202, made)


@router.get(
    "/projects/{project_id}/imports", response=Page[ImportOut], summary="The project's imports"
)
def list_imports(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Newest first."""
    me = caller(request)
    target = project(me, project_id, Action.PROJECT_IMPORT)
    found = BundleImport.objects.filter(project=target).select_related(
        "requested_by", "applied_by", "upload"
    )
    return page(found, params, newest_first=True)


@router.get("/imports/{import_id}", response=ImportOut, summary="An import, and its plan")
def get_import(request: HttpRequest, import_id: UUID) -> BundleImport:
    return _import(request, import_id)


@router.get(
    "/imports/{import_id}/entities",
    response=Page[ImportEntityOut],
    summary="What importing does to each entity",
)
def import_entities(
    request: HttpRequest, import_id: UUID, params: Query[ImportEntityParams]
) -> dict[str, Any]:
    """In the bundle's order; ``action`` narrows them to new ones, conflicts, and so on."""
    made = _import(request, import_id)
    found = ImportEntity.objects.filter(bundle_import=made)
    if params.action:
        found = found.filter(action=params.action)
    return _member_page(made, found, params)


@router.get(
    "/imports/{import_id}/people",
    response=Page[ImportPersonOut],
    summary="Who the bundle's people are here",
)
def import_people(
    request: HttpRequest, import_id: UUID, params: Query[ImportMemberParams]
) -> dict[str, Any]:
    """Matched by email to someone here, or a placeholder who can't sign in."""
    made = _import(request, import_id)
    return _member_page(
        made, ImportPerson.objects.filter(bundle_import=made).select_related("user"), params
    )


@router.post("/imports/{import_id}/apply", response={202: ImportOut}, summary="Apply an import")
def apply_import(request: HttpRequest, import_id: UUID, payload: ApplyIn) -> Status[BundleImport]:
    """Applies its plan, whole, or nothing of it (202). Iki plans it again first: if what
    it would do changed, nothing is imported, and it's planned again, with the new plan."""
    made = _import(request, import_id)
    _storage()
    try:
        applying.apply(caller(request), made, payload.reason, planned_at=payload.planned_at)
    except applying.StalePlan as error:
        raise Problem(409, str(error), code="stale_plan") from None
    except Refused as error:
        if error.field:
            raise _invalid(error) from None
        raise Problem(409, str(error), code="refused") from None
    return Status(202, _imported(made))


@router.delete("/imports/{import_id}", response={204: None}, summary="Discard an import")
def discard_import(request: HttpRequest, import_id: UUID) -> Status[None]:
    """Before it's applied: its archive and plan are deleted, and the record stays."""
    made = _import(request, import_id)
    try:
        imports.discard(caller(request), made)
    except Refused as error:
        raise Problem(409, str(error), code="refused") from None
    return Status(204, None)
