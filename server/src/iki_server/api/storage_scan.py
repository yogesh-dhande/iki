"""Resolve a scan where it will be used, without recording a physical observation."""

from typing import Annotated, Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Router, Schema

from iki_server.accounts.models import User
from iki_server.api.common import caller
from iki_server.api.entities import _find
from iki_server.api.problems import Problem
from iki_server.api.storage import _container, _records
from iki_server.entities import access
from iki_server.storage import scan_identity
from iki_server.storage import services as storage
from iki_server.storage.grids import NotAPosition
from iki_server.storage.models import Place

router = Router(tags=["storage"])
ScanRef = Annotated[str, Field(min_length=1, max_length=64)]


class ScanResolveIn(Schema):
    scope: Literal["box", "confirmation", "destination"]
    code: str = Field(max_length=200, description="The label, printed Iki ID or record link read.")
    container: ScanRef | None = Field(None, description="The container to search for a box scan.")
    position: str | None = Field(
        None, min_length=1, max_length=8, description="Only the usable occupant at this position."
    )
    record: ScanRef | None = Field(
        None, description="The record whose identity is being confirmed."
    )
    records: list[ScanRef] | None = Field(
        None,
        min_length=1,
        max_length=storage.MAX_MOVES,
        description="All records participating in this confirmation, including the target.",
    )
    mode: scan_identity.ScanMode = Field(default_factory=lambda: "label")
    choice: UUID | None = Field(None, description="A readable candidate chosen for navigation.")


class ScanCandidateProjectOut(Schema):
    id: UUID
    name: str


class ScanCandidateBoxOut(Schema):
    id: UUID
    hrid: str
    title: str


class ScanCandidateOut(Schema):
    id: UUID
    hrid: str
    title: str
    type: str
    owner_project: ScanCandidateProjectOut | None
    box: ScanCandidateBoxOut | None
    position: str | None


class ScanResolveOut(Schema):
    status: Literal["one", "ambiguous", "none"]
    candidates: list[ScanCandidateOut]


def _invalid_choice(field: str, message: str) -> Problem:
    return Problem(
        422, "Some fields are invalid.", errors=[{"pointer": f"#/{field}", "detail": message}]
    )


def _in_box(me: User, scan: ScanResolveIn) -> dict[str, Any]:
    if scan.container is None:
        raise _invalid_choice("container", "Choose the container to search.")
    container = _container(me, scan.container)
    places = Place.objects.filter(container=container)
    if scan.position is not None:
        shape = storage.shapes([container.pk]).get(container.pk)
        if shape is None:
            raise _invalid_choice("position", "This container has no numbered positions.")
        try:
            row, column = shape.parse(scan.position)
        except NotAPosition as error:
            raise _invalid_choice("position", str(error)) from None
        places = places.filter(row=row, column=column)
    # The normal box view discloses that the position is taken. Its barcode and
    # any global matches are immaterial when this occupant cannot be read.
    if scan.position is not None and places.exclude(pk__in=access.visible(me)).exists():
        raise scan_identity.ScanRefused()
    candidates = set(places.filter(custodian__isnull=True).values_list("pk", flat=True))
    index = scan_identity.ScanIndex(me, [scan.code], scope_ids=candidates)
    match = index.resolve(
        scan.code,
        mode=scan.mode,
        choice=scan.choice,
        candidates=candidates,
        has_concealed=scan.position is None and bool(candidates - index.readable_ids),
    )
    return index.describe(match)


def _confirmation(me: User, scan: ScanResolveIn) -> dict[str, Any]:
    if scan.record is None:
        raise _invalid_choice("record", "Choose the record being confirmed.")
    target = _find(me, scan.record)
    records = _records(me, scan.records) if scan.records is not None else [target]
    if all(record.pk != target.pk for record in records):
        raise _invalid_choice("records", "Include the record being confirmed in this selection.")
    if scan.choice is not None:
        raise scan_identity.ScanRefused()
    scope = storage.ConfirmationScope(me, records, [scan.code])
    match = scope.resolve(target, scan.code, mode=scan.mode)
    if match.record is not None and match.record.pk != target.pk:
        raise scan_identity.ScanRefused()
    return scope.index.describe(match)


@router.post(
    "/storage/scan/resolve",
    response=ScanResolveOut,
    summary="Identify a scan in its storage context",
)
def resolve_storage_scan(request: HttpRequest, payload: ScanResolveIn) -> dict[str, Any]:
    """Preview a box search, a destination, or the evidence for confirming a selected record.
    Each candidate names the record, project and placement that the caller may read."""
    me = caller(request)
    if payload.scope != "box" and (payload.container is not None or payload.position is not None):
        raise _invalid_choice("container", "Container and position belong to a box search.")
    if payload.scope != "confirmation" and (
        payload.record is not None or payload.records is not None
    ):
        raise _invalid_choice("record", "Selected records belong to a confirmation.")
    if payload.scope == "box":
        return _in_box(me, payload)
    if payload.scope == "confirmation":
        return _confirmation(me, payload)
    index = scan_identity.ScanIndex(me, [payload.code])
    match = index.resolve(
        payload.code, mode=payload.mode, choice=payload.choice, containers_only=True, direct_id=True
    )
    return index.describe(match)
