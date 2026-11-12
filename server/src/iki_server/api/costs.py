"""What runs cost (ADR 0070): across the organization for those who answer for it, and in
each project for anyone who can read it. Budgets limit it (ADR 0071)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from django.db import transaction
from django.db.models import F, QuerySet
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status
from pydantic import field_validator

from iki_server.api.common import caller, project, require
from iki_server.api.problems import Problem
from iki_server.api.schemas import PersonRef
from iki_server.audit import log as audit
from iki_server.org import permissions
from iki_server.org.models import Project, Team
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs import budgets, costs, queue
from iki_server.runs.models import Budget

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


class BudgetStatusOut(Schema):
    amount: float = Field(description="A month's.")
    currency: str
    month: date = Field(description="This month's first day, in UTC.")
    spent: float = Field(description="This month, so far.")
    percent: int
    enforce: bool = Field(description="Once spent, no more container or VM runs start.")
    stopped: bool = Field(description="It's enforced and spent: runs on costly tiers wait.")


class ProjectCostSummaryOut(CostSummaryOut):
    budget: BudgetStatusOut | None = Field(description="The project's own budget, if it has one.")


def _status_out(found: budgets.Status) -> dict[str, Any]:
    return {
        "amount": float(found.budget.amount),
        "currency": found.budget.currency,
        "month": found.month,
        "spent": float(found.spent.quantize(Decimal("0.01"))),
        "percent": found.percent,
        "enforce": found.budget.enforce,
        "stopped": found.stopped,
    }


@router.get(
    "/projects/{project_id}/costs",
    response=ProjectCostSummaryOut,
    summary="What a project's runs cost",
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
    own = Budget.objects.filter(project=found).first()
    return {
        "since": since,
        "until": until,
        "by": params.by,
        **costs.summary(records, params.by),
        "budget": _status_out(budgets.status(own)) if own else None,
    }


# --- Budgets (ADR 0071) ----------------------------------------------------------------------


class BudgetScopeOut(Schema):
    id: UUID
    name: str


class BudgetOut(BudgetStatusOut):
    id: UUID
    team: BudgetScopeOut | None
    project: BudgetScopeOut | None
    warn_at: list[int] = Field(description="Percentages at which to warn, once a month each.")
    created_by: PersonRef
    created_at: datetime
    updated_at: datetime


def _warn_at(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if len(value) > 10 or any(not 1 <= v <= 100 for v in value):
        raise ValueError("Give at most 10 percentages, each from 1 to 100.")
    return sorted(set(value))


class BudgetIn(Schema):
    team: UUID | None = Field(
        None, description="A team's runs. Leave out team and project for the organization's."
    )
    project: UUID | None = Field(None, description="A project's runs.")
    amount: Decimal = Field(gt=0, le=Decimal("1e12"), decimal_places=2, description="A month's.")
    warn_at: list[int] = Field(default_factory=lambda: [50, 80, 100])
    enforce: bool = Field(
        False, description="Once it's spent, no more container or VM runs start that month."
    )

    @field_validator("warn_at")
    @classmethod
    def _check_warn_at(cls, value: list[int]) -> list[int]:
        checked = _warn_at(value)
        assert checked is not None  # noqa: S101 - given
        return checked


class BudgetPatch(Schema):
    amount: Decimal | None = Field(None, gt=0, le=Decimal("1e12"), decimal_places=2)
    warn_at: list[int] | None = None
    enforce: bool | None = None

    @field_validator("warn_at")
    @classmethod
    def _check_warn_at(cls, value: list[int] | None) -> list[int] | None:
        return _warn_at(value)


def _budgets() -> QuerySet[Budget]:
    return Budget.objects.select_related("team", "project", "created_by")


def _budget(budget_id: UUID) -> Budget:
    found = _budgets().filter(pk=budget_id).first()
    if found is None:
        raise Problem(404, "No such budget.")
    return found


def _budget_out(budget: Budget) -> dict[str, Any]:
    return {
        **_status_out(budgets.status(budget)),
        "id": budget.pk,
        "team": {"id": budget.team.pk, "name": budget.team.name} if budget.team else None,
        "project": (
            {"id": budget.project.pk, "name": budget.project.name} if budget.project else None
        ),
        "warn_at": budget.warn_at,
        "created_by": budget.created_by,
        "created_at": budget.created_at,
        "updated_at": budget.updated_at,
    }


def _terms(budget: Budget) -> dict[str, Any]:
    return {
        "amount": str(Decimal(budget.amount).quantize(Decimal("0.01"))),
        "currency": budget.currency,
        "warn_at": budget.warn_at,
        "enforce": budget.enforce,
    }


def _invalid(pointer: str, detail: str) -> Problem:
    return Problem(422, "Some fields are invalid.", errors=[{"pointer": pointer, "detail": detail}])


def _released() -> None:
    # A raised or removed budget may let waiting runs start.
    for tier in costs.PRICED_TIERS:
        queue.notify(tier)


@router.get("/compute/budgets", response=list[BudgetOut], summary="Budgets")
def list_budgets(request: HttpRequest) -> list[dict[str, Any]]:
    """Every budget, the organization's first, with what's been spent this month."""
    require(caller(request), Action.COSTS_READ)
    ordered = _budgets().order_by(
        F("project").asc(nulls_first=True), F("team").asc(nulls_first=True), "id"
    )
    return [_budget_out(b) for b in ordered]


@router.post("/compute/budgets", response={201: BudgetOut}, summary="Set a budget")
def create_budget(request: HttpRequest, payload: BudgetIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """One per scope: the organization, a team or a project. It's in the currency of the
    deployment's prices. Enforcing it stops new container and VM runs once it's spent; runs
    already going finish."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    if payload.team is not None and payload.project is not None:
        raise _invalid("#/project", "Choose a team or a project, not both.")
    team = Team.objects.filter(pk=payload.team).first() if payload.team else None
    found = Project.objects.filter(pk=payload.project).first() if payload.project else None
    if payload.team is not None and team is None:
        raise _invalid("#/team", "No such team.")
    if payload.project is not None and found is None:
        raise _invalid("#/project", "No such project.")
    code = budgets.currency()
    if code is None:
        raise Refused(
            "No compute template has a price yet, so runs cost nothing to budget for. Give "
            "the container and VM templates a `price` first."
        )
    if Budget.objects.filter(team=team, project=found).exists():
        raise Refused("There's a budget for this already: change that one.")
    with transaction.atomic():
        budget = Budget.objects.create(
            team=team,
            project=found,
            amount=payload.amount,
            currency=code,
            warn_at=payload.warn_at,
            enforce=payload.enforce,
            created_by=me,
        )
        audit.record(
            "budget.created",
            actor=me,
            target=budget,
            project=found,
            team=team,
            data=_terms(budget),
        )
    response["Location"] = f"/api/v1/compute/budgets/{budget.pk}"
    return Status(201, _budget_out(_budget(budget.pk)))


@router.patch("/compute/budgets/{budget_id}", response=BudgetOut, summary="Change a budget")
def update_budget(request: HttpRequest, budget_id: UUID, payload: BudgetPatch) -> dict[str, Any]:
    """Changes what's given. A raised budget lets waiting runs start; warnings already sent
    this month aren't sent again."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    with transaction.atomic():
        budget = Budget.objects.select_for_update().get(pk=_budget(budget_id).pk)
        before = _terms(budget)
        if payload.amount is not None:
            budget.amount = payload.amount
        if payload.warn_at is not None:
            budget.warn_at = payload.warn_at
        if payload.enforce is not None:
            budget.enforce = payload.enforce
        budget.updated_at = timezone.now()
        budget.save(update_fields=["amount", "warn_at", "enforce", "updated_at"])
        audit.record(
            "budget.updated",
            actor=me,
            target=budget,
            project=budget.project,
            team=budget.team,
            data={"before": before, "after": _terms(budget)},
        )
    _released()
    return _budget_out(_budget(budget.pk))


@router.delete("/compute/budgets/{budget_id}", response={204: None}, summary="Remove a budget")
def delete_budget(request: HttpRequest, budget_id: UUID) -> Any:  # noqa: ANN401
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    budget = _budget(budget_id)
    with transaction.atomic():
        audit.record(
            "budget.removed",
            actor=me,
            target=budget,
            project=budget.project,
            team=budget.team,
            data=_terms(budget),
        )
        budget.delete()
    _released()
    return Status(204, None)
