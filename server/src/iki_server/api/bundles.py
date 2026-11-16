"""Bundles: a project's entities, with their history, for another deployment (ADR 0096)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema
from ninja.responses import Status

from iki_server.api.common import caller, project
from iki_server.api.files import _invalid, _storage
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.bundles import exports
from iki_server.bundles.models import BundleExport
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
    outside: int = Field(description="Entities its values refer to that aren't in it.")


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
