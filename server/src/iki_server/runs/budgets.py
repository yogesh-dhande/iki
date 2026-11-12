"""Budgets (ADR 0071): what the organization, a team or a project may spend on runs'
machines in a month.

- **Spent** is what's recorded this month (ADR 0070) in the budget's scope, and what the
  machines still working for its runs have cost so far. An organization's budget counts
  idle machines too, once their time is recorded.
- **Warnings**: as spending passes each of a budget's ``warn_at`` percentages, its Compute
  Admins (and a project's Admins, for a project's budget) are told, once a month each,
  and it's audited and sent to webhooks.
- **Enforced** budgets stop spending once they're spent: no more runs on the container
  and VM tiers start in their scope until the month ends or the budget is raised. Runs
  already going finish. A launch that would likely take it past the budget (its runs'
  typical cost) is refused.

Months are calendar months in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import catalog, costs
from iki_server.runs.models import Budget, BudgetAlert, CostRecord, Job, LaunchState

if TYPE_CHECKING:
    from iki_server.runs.quotas import Blocked
    from iki_spec.model import Action

__all__ = ["check_launch", "currency", "hold", "month_of", "send_alerts", "spent", "status"]


def month_of(now: datetime) -> tuple[date, datetime, datetime]:
    """The month `now` is in, in UTC: its first day, and when it starts and ends."""
    first = now.astimezone(UTC).date().replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return (
        first,
        datetime(first.year, first.month, 1, tzinfo=UTC),
        datetime(following.year, following.month, 1, tzinfo=UTC),
    )


def currency() -> str | None:
    """The currency the deployment's prices are in; None while nothing has a price."""
    found = {t.currency for t in catalog.compute().templates.values() if t.currency}
    return min(found) if found else None


def _in_scope(budget: Budget, *, team: str) -> Q:
    """What's in the budget's scope, where `team` is the field naming the team."""
    if budget.project_id is not None:
        return Q(project_id=budget.project_id)
    if budget.team_id is not None:
        return Q(**{team: budget.team_id})
    return Q()


def _ongoing(budget: Budget, now: datetime) -> Decimal:
    """What machines still working for the scope's runs have cost so far."""
    jobs = (
        Job.objects.filter(packed_into__isnull=True, started_at__isnull=False)
        .exclude(worker__launch=None)
        .exclude(worker__launch__state=LaunchState.ENDED)
        .filter(_in_scope(budget, team="project__team_id"))
        .select_related("worker__launch")
    )
    total = Decimal(0)
    for job in jobs:
        going = costs._going(job, now)
        if going is None:
            continue
        launch, since = going
        price = costs.price(launch.template)
        if price is not None and price.currency == budget.currency:
            total += price.of(costs._seconds(since, now))
    return total


def spent(budget: Budget, now: datetime | None = None) -> Decimal:
    """What's been spent in the budget's scope this month, so far."""
    now = now or timezone.now()
    _, start, end = month_of(now)
    recorded = (
        CostRecord.objects.filter(ended_at__gte=start, ended_at__lt=end, currency=budget.currency)
        # A record keeps the team its project was in then.
        .filter(_in_scope(budget, team="team_id"))
        .aggregate(total=Sum("amount"))["total"]
    )
    return (recorded or Decimal(0)) + _ongoing(budget, now)


@dataclass(frozen=True)
class Status:
    budget: Budget
    month: date
    spent: Decimal

    @property
    def percent(self) -> int:
        return int(self.spent * 100 / self.budget.amount)

    @property
    def stopped(self) -> bool:
        return self.budget.enforce and self.spent >= self.budget.amount


def status(budget: Budget, now: datetime | None = None) -> Status:
    now = now or timezone.now()
    return Status(budget, month_of(now)[0], spent(budget, now))


def covering(project: Project) -> QuerySet[Budget]:
    """The project's own budget, its team's and the organization's."""
    return Budget.objects.filter(
        Q(project=project) | Q(team_id=project.team_id) | Q(project=None, team=None)
    )


def money(amount: Decimal, code: str) -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(code)
    text = f"{amount.quantize(Decimal('0.01')):,}"
    return f"{symbol}{text}" if symbol else f"{text} {code}"


def _whose(budget: Budget) -> str:
    if budget.project_id is not None:
        return "The project’s"
    if budget.team_id is not None:
        return "Its team’s"
    return "The organization’s"


def _month_name(month: date) -> str:
    return month.strftime("%B")


# --- Stopping spending -----------------------------------------------------------------------


def hold(stop: Blocked, tier: str, now: datetime | None = None) -> None:
    """Adds the scopes whose enforced budgets are spent to `stop`, on tiers that cost."""
    if tier not in costs.PRICED_TIERS:
        return
    for budget in Budget.objects.filter(enforce=True):
        found = status(budget, now)
        if not found.stopped:
            continue
        why = (
            f"{_whose(budget)} budget for {_month_name(found.month)} is spent "
            f"({money(found.spent, budget.currency)} of {money(budget.amount, budget.currency)})."
        )
        if budget.project_id is not None:
            stop.projects.setdefault(budget.project_id, why)
        elif budget.team_id is not None:
            stop.teams.setdefault(budget.team_id, why)
        else:
            stop.organization = stop.organization or why


def check_launch(project: Project, action: Action, runs: int = 1) -> None:
    """Refuses a launch on a costly tier that its scope's enforced budgets can't pay for:
    one that's spent, or that its runs' typical cost would take past it."""
    if action.tier not in costs.PRICED_TIERS:
        return
    budgets = list(covering(project).filter(enforce=True))
    if not budgets:
        return
    estimate = costs.estimate(action)
    typical = Decimal(str(estimate["typical"])) if estimate and estimate["typical"] else None
    for budget in budgets:
        found = status(budget)
        month = _month_name(found.month)
        if found.stopped:
            raise Refused(
                f"{_whose(budget)} budget for {month} is spent "
                f"({money(found.spent, budget.currency)} of "
                f"{money(budget.amount, budget.currency)}), so runs on the {action.tier} tier "
                "can't start until next month. A Compute Admin can raise it."
            )
        if typical is None or estimate is None or estimate["currency"] != budget.currency:
            continue
        left = budget.amount - found.spent
        likely = typical * runs
        if likely > left:
            several = f"these {runs} runs typically cost" if runs > 1 else "a run typically costs"
            raise Refused(
                f"{_whose(budget)} budget for {month} has {money(left, budget.currency)} left, "
                f"and {several} about {money(likely, budget.currency)}. Launch fewer, or ask "
                "a Compute Admin to raise it."
            )


# --- Warnings --------------------------------------------------------------------------------


def _recipients(budget: Budget) -> list[Any]:
    from iki_server.accounts.models import User
    from iki_server.org.models import OrgRole, OrgRoleAssignment, ProjectMembership, ProjectRole

    ids = set(
        OrgRoleAssignment.objects.filter(role=OrgRole.COMPUTE_ADMIN).values_list("user", flat=True)
    )
    if budget.project_id is not None:
        ids |= set(
            ProjectMembership.objects.filter(
                project_id=budget.project_id, role=ProjectRole.ADMIN, user__isnull=False
            ).values_list("user", flat=True)
        )
    return list(User.objects.filter(pk__in=ids, is_active=True))


def send_alerts(now: datetime | None = None) -> int:
    """Tells of each threshold a budget's spending passed this month, once. Any task runner
    may call it: a threshold is told by whichever records it first. Returns how many."""
    from iki_server.notifications import kinds
    from iki_server.notifications.services import notify

    now = now or timezone.now()
    told = 0
    for budget in Budget.objects.select_related("project", "team"):
        found = status(budget, now)
        passed = sorted(t for t in budget.warn_at if found.percent >= t)
        done = set(
            BudgetAlert.objects.filter(budget=budget, month=found.month).values_list(
                "threshold", flat=True
            )
        )
        new = [t for t in passed if t not in done]
        if not new:
            continue
        threshold = new[-1]  # the highest passed: one message, not several at once
        try:
            with transaction.atomic():
                for each in new:
                    BudgetAlert.objects.create(
                        budget=budget, month=found.month, threshold=each, spent=found.spent
                    )
                _tell(budget, found, threshold, notify, kinds.BUDGET_THRESHOLD)
        except IntegrityError:
            continue  # another runner told it first
        told += 1
    return told


def scope_name(budget: Budget) -> str:
    if budget.project is not None:
        return budget.project.name
    if budget.team is not None:
        return f"the {budget.team.name} team"
    return "the organization"


def _tell(budget: Budget, found: Status, threshold: int, notify: Any, kind: Any) -> None:  # noqa: ANN401
    month = _month_name(found.month)
    spent_text = money(found.spent, budget.currency)
    amount_text = money(budget.amount, budget.currency)
    title = (
        f"The budget for {scope_name(budget)} is spent for {month}"
        if threshold >= 100
        else f"{threshold}% of the budget for {scope_name(budget)} is spent for {month}"
    )
    body = f"{spent_text} of {amount_text} spent on runs’ machines so far this month." + (
        " No more runs on the container and VM tiers start in its scope until next month, or "
        "until it's raised."
        if budget.enforce and threshold >= 100
        else ""
    )
    audit.record(
        "budget.threshold_reached",
        target=budget,
        project=budget.project,
        team=budget.team,
        data={
            "threshold": threshold,
            "month": found.month.isoformat(),
            "spent": str(found.spent.quantize(Decimal("0.01"))),
            "amount": str(budget.amount),
            "currency": budget.currency,
            "enforce": budget.enforce,
        },
    )
    path = f"/projects/{budget.project.slug}/" if budget.project is not None else "/costs/"
    notify(
        _recipients(budget),
        kind,
        title=title,
        body=body,
        path=path,
        project=budget.project,
    )
