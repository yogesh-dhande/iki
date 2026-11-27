"""People in records (ADR 0122): whom a project's records can name, what's assigned to
you, taking a record, and reassigning many at once."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Case, Q, Value, When
from django.db.models.functions import Coalesce, Collate, Lower, NullIf
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema

from iki_server.api import preconditions
from iki_server.api.common import caller, project
from iki_server.api.entities import EntityOut, _find, _invalid, _one, _out
from iki_server.api.pagination import PageParams, page_by
from iki_server.api.preconditions import IfMatch
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.entities import assignments, people, registry, services
from iki_server.entities.values import Invalid, shape
from iki_server.org import lab_roles
from iki_server.org.permissions import Action
from iki_spec.model import names_people, person_role

router = Router(tags=["work"])


# --- Whom records can name ------------------------------------------------------------------------


class NameableOut(Schema):
    id: UUID
    name: str
    email: str


class NameableFilters(PageParams):
    q: str = Field("", max_length=200, description="Part of their name or email address.")
    role: str | None = Field(
        None, description="Only those holding this lab role in the project, qualified."
    )
    entity_type: str | None = Field(
        None, description="With `field`: only those that field of this entity type can name."
    )
    field: str | None = Field(None, description="With `entity_type`: one of its person fields.")


@router.get(
    "/projects/{project_id}/people",
    response=Page[NameableOut],
    summary="Whom its records can name",
)
def nameable(
    request: HttpRequest, project_id: UUID, filters: Query[NameableFilters]
) -> dict[str, Any]:
    """The people its records' fields can name (ADR 0122), for a picker: active people
    who can read it, holding the field's lab role if it has one. Service accounts and
    placeholders can't be named. Best matches first: those whose name, a word of it, or
    email address starts with ``q``, then the rest, each by name. It needs you to read the
    project."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_READ)
    role = filters.role
    if filters.entity_type is not None or filters.field is not None:
        entity_type = registry.types().get(filters.entity_type or "")
        f = None
        if entity_type is not None and filters.field:
            f = shape(entity_type.qualified_name).fields().get(filters.field)
        if f is None or not names_people(f):
            raise Problem(404, "No such field.")
        role = person_role(f)
    if role is not None and role not in lab_roles.declared():
        raise Problem(404, "No such lab role.")
    candidates = people.nameable(found, role)
    text = filters.q.strip()
    # The web app's picker works out this answer from an earlier, wider one while it waits
    # (narrowNameable in web/app/src/people.ts): a change to the match or order goes there too.
    if text:
        candidates = candidates.filter(Q(name__icontains=text) | Q(email__icontains=text))
        starts = (
            Q(name__istartswith=text) | Q(email__istartswith=text) | Q(name__icontains=f" {text}")
        )
        rank: Case | Value = Case(When(starts, then=Value(0)), default=Value(1))
    else:
        rank = Value(0)
    ranked = candidates.only("pk", "name", "email").annotate(
        match_rank=rank,
        # By name as shown, in code point order, as the picker's own sort compares them.
        shown_name=Collate(Lower(Coalesce(NullIf("name", Value("")), "email")), "C"),
    )
    paged = page_by(ranked, filters, by=("match_rank", "shown_name", "pk"))
    return {
        **paged,
        "items": [
            {"id": u.pk, "name": u.name or u.email, "email": u.email} for u in paged["items"]
        ],
    }


# --- Work -----------------------------------------------------------------------------------------


class WorkItemOut(Schema):
    entity: EntityOut
    fields: list[str] = Field(
        description="The assignment fields naming you that count in its states; for open "
        "work, the ones you could take it in."
    )
    due: dt.date | dt.datetime | None = Field(
        description="When it's due, from its type's `due` field, if it has one."
    )


class WorkFilters(PageParams):
    project: list[UUID] = Field(default_factory=list, description="Only these projects'.")
    type: list[str] = Field(
        default_factory=list, description="Only these types' records (and their subtypes')."
    )
    state: list[str] = Field(
        default_factory=list,
        description="Only records whose state fields (ADR 0125) are in these states, each "
        "`field.state` such as `status.in_review`: in any of a field's, and of every field "
        "named. Types without those state fields have none.",
    )
    sort: Literal["due", "updated"] = Field(
        "due",
        description="`due`: soonest due first, undated ones last; `updated`: latest changed first.",
    )


def _work(request: HttpRequest, filters: WorkFilters, *, open_: bool) -> dict[str, Any]:
    me = caller(request)
    by_field: dict[str, list[str]] = {}
    for given in filters.state:
        name, dot, state = given.partition(".")
        if not dot or not name or not state:
            raise Problem(
                422,
                "Some fields are invalid.",
                errors=[
                    {
                        "parameter": "state",
                        "detail": f"`{given}` isn't a state field's state: say it as "
                        "`field.state`, such as `status.in_review`.",
                    }
                ],
            )
        by_field.setdefault(name, []).append(state)
    wanted = assignments.WorkFilters(
        projects=tuple(filters.project),
        types=tuple(t for t in filters.type if t in registry.types()),
        states={n: tuple(v) for n, v in by_field.items()},
        sort=filters.sort,
    )
    if filters.type and not wanted.types:
        return {"items": [], "next_cursor": None}
    lister = assignments.open_work if open_ else assignments.work
    try:
        items, next_cursor = lister(me, wanted, filters.cursor, filters.limit)
    except assignments.BadCursor as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"parameter": "cursor", "detail": str(error)}],
        ) from None
    shown = {e["id"]: e for e in _out(me, [i.entity for i in items])}
    return {
        "items": [
            {"entity": shown[i.entity.pk], "fields": list(i.fields), "due": i.due} for i in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/me/work-items", response=Page[WorkItemOut], summary="What's assigned to you")
def list_work_items(request: HttpRequest, filters: Query[WorkFilters]) -> dict[str, Any]:
    """Everything assigned to you now (ADR 0122), across the projects you read: records an
    assignment field names you in, while their state fields are in states it counts in. You
    drop off a record's work when you can't be named in it any more (you left its project,
    say)."""
    return _work(request, filters, open_=False)


@router.get(
    "/me/open-work-items", response=Page[WorkItemOut], summary="Work open to your lab roles"
)
def list_open_work_items(request: HttpRequest, filters: Query[WorkFilters]) -> dict[str, Any]:
    """Records nobody is assigned that one of your lab roles could take (ADR 0122): an
    assignment field limited to a role you hold in the record's project counts in its
    states, and names nobody who can be named there now. Take one with `POST
    /entities/{ref}/take`, or simply do it: a locked record's people can't change."""
    return _work(request, filters, open_=True)


# --- Taking and reassigning -----------------------------------------------------------------------


class TakeIn(Schema):
    field: str = Field(description="One of the record's `can.take` fields.")
    reason: str = Field("", max_length=2000, description="Why, for the record.")


@router.post(
    "/entities/{ref}/take",
    response=EntityOut,
    summary="Assign a record to yourself",
    openapi_extra=preconditions.changes(),
)
def take(
    request: HttpRequest,
    ref: str,
    payload: TakeIn,
    response: HttpResponse,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    """Names you in an assignment field limited to one of your lab roles (ADR 0122),
    while it names nobody who can be named: open work, as `can.take` lists it. A revision,
    like any edit. Send If-Match with the ETag you read."""
    me = caller(request)
    entity = _find(me, ref)
    expected = preconditions.expected(if_match)
    try:
        assignments.take(
            me, entity, payload.field, expected_revision=expected, reason=payload.reason
        )
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except services.Stale as stale:
        raise preconditions.stale(
            stale.current,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then take it.",
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    return _one(me, _find(me, str(entity.pk)), response)


class AssignIn(Schema):
    entities: list[UUID] = Field(min_length=1, max_length=assignments.MAX_REASSIGN)
    expected_revisions: dict[UUID, Annotated[int, Field(ge=1, strict=True)]] | None = Field(
        None,
        description="Required. Each selected entity's observed revision, by entity ID.",
    )
    field: str = Field(description="An assignment field of theirs.")
    person: str | None = Field(
        description="Whom to name: an ID or email address; null to empty the field."
    )
    op: Literal["set", "add", "remove"] = Field(
        "set",
        description="`set` names only them; for a list, `add` them or `remove` them. "
        "`remove` on a single person's field empties it if it names them.",
    )
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class SkippedOut(Schema):
    entity: UUID
    hrid: str
    why: str


class AssignOut(Schema):
    changed: list[UUID] = Field(description="The records changed, each with a revision.")
    skipped: list[SkippedOut] = Field(description="Those left as they were, and why.")
    group: str = Field(
        description="Names this reassignment in each change's audit record (`entity.assigned`)."
    )


@router.post(
    "/entities/assign",
    response=AssignOut,
    summary="Reassign many records",
    openapi_extra=preconditions.changes(tagged=False),
)
def assign(request: HttpRequest, payload: AssignIn) -> dict[str, Any]:
    """Names someone in an assignment field of many records at once (ADR 0122), such as a
    grid's selection: each record you may edit changes as its own revision, and each person
    is told once. Records that can't change are skipped, with why: you can't edit them,
    their state locks them, their people with them, or the person can't be named
    in their project. Send an observed revision in `expected_revisions` for every selected
    entity. A stale selection answers 412 and identifies the changed records. A record
    you can't read answers 404. Neither refusal changes any records."""
    me = caller(request)
    revisions = payload.expected_revisions
    if revisions is None:
        raise preconditions.required(
            "Send the revision you observed for each entity in expected_revisions."
        )
    if set(revisions) != set(payload.entities):
        raise Problem(
            422,
            "The revisions must match the selection.",
            errors=[
                {
                    "pointer": "#/expected_revisions",
                    "detail": "Send the observed revision for exactly the selected entities.",
                }
            ],
        )
    try:
        done = assignments.reassign(
            me,
            {pk: revisions[pk] for pk in payload.entities},
            payload.field,
            payload.person,
            op=payload.op,
            reason=payload.reason,
        )
    except assignments.MissingAssignmentRecordsError as missing:
        raise Problem(
            404,
            "No such entities, or you can't read them: "
            + ", ".join(sorted(str(pk) for pk in missing.ids))
            + ".",
        ) from None
    except assignments.StaleAssignmentsError as stale:
        raise Problem(
            412,
            "These records changed since you read them: "
            + ", ".join(record.hrid for record in stale.records)
            + ". Refresh the selection and review it before reassigning.",
            code="stale",
            extensions={
                "stale": [
                    {
                        "id": str(record.pk),
                        "hrid": record.hrid,
                        "expected_revision": revisions[record.pk],
                        "current_revision": record.revision,
                    }
                    for record in stale.records
                ]
            },
        ) from None
    return {
        "changed": [e.pk for e in done.changed],
        "skipped": [{"entity": s.entity, "hrid": s.hrid, "why": s.why} for s in done.skipped],
        "group": done.group,
    }


__all__ = ["router"]
