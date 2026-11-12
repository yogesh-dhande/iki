"""What runs cost (ADR 0070): across the organization for those who answer for it, and in
each project for anyone who can read it."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, StreamingHttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema

from iki_server.api.common import caller, project, require
from iki_server.api.problems import Problem
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.runs import costs

router = Router(tags=["costs"])

#: The longest period one report covers.
MAX_PERIOD = timedelta(days=366)


class PeriodParams(Schema):
    since: date | None = Field(
        None, description="The first day, in UTC. The first of this month unless given."
    )
    until: date | None = Field(
        None, description="The day after the last, in UTC. The first of next month unless given."
    )


class OrgParams(PeriodParams):
    by: Literal["project", "team", "template", "action", "tier"] = "project"


class ProjectParams(PeriodParams):
    by: Literal["action", "template"] = "action"


class CostTotalOut(Schema):
    currency: str | None = Field(description="Null for time on templates without a price.")
    amount: float
    seconds: float = Field(description="Machine time.")


class CostGroupOut(Schema):
    key: str | None = Field(
        description="The project's or team's ID, or the template, action or tier. Null for "
        "idle machines: the organization's, not any project's."
    )
    label: str
    currency: str | None
    amount: float
    seconds: float
    runs: int


class CostSummaryOut(Schema):
    since: date
    until: date
    by: str
    totals: list[CostTotalOut] = Field(description="By currency.")
    groups: list[CostGroupOut] = Field(description="Most first.")


def _period(params: PeriodParams) -> tuple[date, date, datetime, datetime]:
    today = timezone.now().astimezone(UTC).date()
    since = params.since or today.replace(day=1)
    until = params.until or (since.replace(day=1) + timedelta(days=32)).replace(day=1)
    if until <= since:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/until", "detail": "Make it later than since."}],
        )
    if until - since > MAX_PERIOD:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/until", "detail": "A report covers at most a year."}],
        )
    start = datetime.combine(since, time(), UTC)
    end = datetime.combine(until, time(), UTC)
    return since, until, start, end


@router.get("/compute/costs", response=CostSummaryOut, summary="What runs cost")
def org_costs(request: HttpRequest, params: Query[OrgParams]) -> dict[str, Any]:
    """For Compute Admins, Auditors and Org Admins: what the machines Iki started cost in a
    period, by project, team, template, action or tier. A machine's cost counts when it's
    recorded: when it ends, or when a persistent one lets go of a job."""
    require(caller(request), Action.COSTS_READ)
    since, until, start, end = _period(params)
    found = costs.summary(costs.between(start, end), params.by)
    return {"since": since, "until": until, "by": params.by, **found}


@router.get(
    "/compute/costs/records.csv",
    summary="Download what runs cost",
    openapi_extra={
        "responses": {200: {"description": "The records, as CSV", "content": {"text/csv": {}}}}
    },
)
def org_cost_records(request: HttpRequest, params: Query[PeriodParams]) -> StreamingHttpResponse:
    """Every cost record in the period, oldest first, for finance and audits."""
    require(caller(request), Action.COSTS_READ)
    since, until, start, end = _period(params)
    records = (
        costs.between(start, end)
        .select_related("run", "project", "team")
        .order_by("ended_at", "id")
    )
    columns = [
        "kind",
        "run",
        "action",
        "project_id",
        "project",
        "team_id",
        "team",
        "template",
        "tier",
        "started_at",
        "ended_at",
        "seconds",
        "price_per_hour",
        "currency",
        "amount",
    ]

    def rows() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for record in records.iterator(chunk_size=1000):
            writer.writerow(
                [
                    record.kind,
                    record.run.hrid if record.run else "",
                    record.action,
                    record.project_id or "",
                    _safe(record.project.name) if record.project else "",
                    record.team_id or "",
                    _safe(record.team.name) if record.team else "",
                    record.template,
                    record.tier,
                    record.started_at.isoformat(),
                    record.ended_at.isoformat(),
                    record.seconds,
                    "" if record.price_per_hour is None else record.price_per_hour,
                    record.currency,
                    "" if record.amount is None else record.amount,
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        yield buffer.getvalue()

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="iki-costs-{since}-{until}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _safe(text: str) -> str:
    """A name as a spreadsheet shows it, never as a formula it runs."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get(
    "/projects/{project_id}/costs", response=CostSummaryOut, summary="What a project's runs cost"
)
def project_costs(
    request: HttpRequest, project_id: UUID, params: Query[ProjectParams]
) -> dict[str, Any]:
    """For anyone who can read the project, and those who can read the organization's costs:
    what its runs cost in a period, by action or template. Each run shows its own cost
    too."""
    user = caller(request)
    found = project(user, project_id)
    if not permissions.can(user, Action.COSTS_READ):
        require(user, Action.PROJECT_READ, found)
    since, until, start, end = _period(params)
    records = costs.between(start, end).filter(project=found)
    return {"since": since, "until": until, "by": params.by, **costs.summary(records, params.by)}
