"""Costs (ADR 0070): what the machines Iki starts for runs cost, and whose it was.

A container-tier pod (and, later, a VM) costs its template's ``price.per_hour``
for as long as it's up, whether or not it's running a job. So:

- An **ephemeral** machine runs one job: all of its life, from being asked for
  to ending, is that run's, recorded when the machine ends. One that never ran
  a job is the organization's (``idle``).
- A **persistent** machine runs one job after another: the time it held each
  attempt is that run's, recorded when its worker lets go of it; the rest of its
  life is ``idle``, recorded when it ends.
- A job that ran several runs of a batch at once (ADR 0055) is split evenly
  between them.

Each record keeps the price it was recorded at, and the project, team and
action, as they were. Backend and client tiers have no per-run cost: their
machines run anyway.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Count, F, Q, QuerySet, Sum
from django.utils import timezone

from iki_server.runs import catalog
from iki_server.runs.models import (
    ActionRun,
    CostKind,
    CostRecord,
    Job,
    JobState,
    Launch,
    LaunchState,
    RunStatus,
    Worker,
)
from iki_spec.model import Action

__all__ = [
    "PRICED_TIERS",
    "Price",
    "estimate",
    "launch_ended",
    "of_run",
    "price",
    "released",
    "summary",
]

#: Tiers whose machines Iki starts, and so whose runs cost something.
PRICED_TIERS = ("container", "vm")
#: Past runs an estimate looks at, newest first.
_ESTIMATE_FROM = 50
_HELD = (JobState.LEASED, JobState.STAGING, JobState.RUNNING, JobState.UPLOADING)


@dataclass(frozen=True)
class Price:
    per_hour: Decimal
    currency: str

    def of(self, seconds: Decimal) -> Decimal:
        return (seconds * self.per_hour / 3600).quantize(Decimal("0.0001"), ROUND_HALF_UP)


def price(template: str) -> Price | None:
    """The template's price now, as the deployment's compute says; None if it has none."""
    found = catalog.compute().templates.get(template)
    if found is None or found.price_per_hour is None or not found.currency:
        return None
    return Price(Decimal(str(found.price_per_hour)), found.currency)


def _seconds(start: datetime, end: datetime) -> Decimal:
    return max(Decimal(0), Decimal(str((end - start).total_seconds()))).quantize(Decimal("0.001"))


def _record(
    launch: Launch,
    start: datetime,
    end: datetime,
    *,
    job: Job | None,
    share: int = 1,
    seconds: Decimal | None = None,
) -> CostRecord:
    if seconds is None:
        seconds = (_seconds(start, end) / share).quantize(Decimal("0.001"))
    costs = price(launch.template)
    whose: dict[str, Any] = {}
    if job is not None:
        run = job.run
        whose = {
            "job": job,
            "run": run,
            "project": run.project,
            "team_id": run.project.team_id,
            "action": run.action,
        }
    return CostRecord.objects.create(
        launch=launch,
        kind=CostKind.RUN if job is not None else CostKind.IDLE,
        template=launch.template,
        tier=job.tier if job is not None else _tier(launch),
        started_at=start,
        ended_at=end,
        seconds=seconds,
        price_per_hour=costs.per_hour if costs else None,
        currency=costs.currency if costs else "",
        amount=costs.of(seconds) if costs else None,
        **whose,
    )


def _tier(launch: Launch) -> str:
    template = catalog.compute().templates.get(launch.template)
    return template.tier if template is not None else "container"


def _runs(launch: Launch, job: Job, start: datetime, end: datetime) -> None:
    """Records the machine's time from `start` to `end` as the job's runs', split evenly
    between those packed into it."""
    attempts = [job, *Job.objects.filter(packed_into=job).select_related("run__project")]
    for attempt in attempts:
        _record(launch, start, end, job=attempt, share=len(attempts))


def released(job: Job, at: datetime | None = None) -> None:
    """The worker let go of the attempt. On a persistent machine, the time it held it is
    its runs'. Call inside the transaction that ends it."""
    if job.packed_into_id is not None or job.started_at is None or job.worker_id is None:
        return
    worker = Worker.objects.select_related("launch").filter(pk=job.worker_id).first()
    launch = worker.launch if worker is not None else None
    if launch is None or launch.once:
        return
    _runs(launch, job, job.started_at, at or timezone.now())


def launch_ended(launch: Launch) -> None:
    """Records what's left of the machine's cost once it has ended. Call inside the
    transaction that ends it, after its worker's jobs are let go."""
    assert launch.ended_at is not None  # noqa: S101 - it has ended
    start, end = launch.created_at, launch.ended_at
    if launch.once:
        ran = (
            Job.objects.filter(worker__launch=launch, packed_into__isnull=True)
            .exclude(started_at=None)
            .select_related("run__project")
            .order_by("started_at")
            .first()
        )
        if ran is not None:
            _runs(launch, ran, start, end)
        else:
            _record(launch, start, end, job=None)
        return
    used = launch.costs.filter(kind=CostKind.RUN).aggregate(s=Sum("seconds"))["s"] or Decimal(0)
    idle = _seconds(start, end) - used
    if idle > 0:
        # What's left of the machine's life, besides its runs' time.
        _record(launch, start, end, job=None, seconds=idle.quantize(Decimal("0.001")))


# --- What a run costs ----------------------------------------------------------------------


def _going(job: Job, now: datetime) -> tuple[Launch, datetime] | None:
    """For an attempt whose machine is still up and still its: the machine, and when its
    time started counting as the attempt's."""
    worker = job.worker
    launch = worker.launch if worker is not None else None
    if launch is None or launch.state == LaunchState.ENDED or job.started_at is None:
        return None
    if launch.once:
        return launch, launch.created_at
    if job.state in _HELD:
        return launch, job.started_at
    return None


def of_run(run: ActionRun) -> dict[str, Any] | None:
    """What the run has cost: recorded, and so far on machines still up. None for a run on a
    tier without per-run costs."""
    if run.tier not in PRICED_TIERS:
        return None
    now = timezone.now()
    recorded = run.costs.aggregate(amount=Sum("amount"), seconds=Sum("seconds"))
    amount = recorded["amount"] or Decimal(0)
    seconds = recorded["seconds"] or Decimal(0)
    currency = run.costs.exclude(currency="").values_list("currency", flat=True).first() or ""
    final = run.finished
    for job in Job.objects.filter(run=run, packed_into__isnull=True).select_related(
        "worker__launch"
    ):
        going = _going(job, now)
        if going is None:
            continue
        final = False
        launch, since = going
        share = 1 + Job.objects.filter(packed_into=job).count()
        part = (_seconds(since, now) / share).quantize(Decimal("0.001"))
        seconds += part
        costs = price(launch.template)
        if costs is not None:
            amount += costs.of(part)
            currency = currency or costs.currency
    return {
        "amount": float(amount.quantize(Decimal("0.0001"))),
        "currency": currency or None,
        "seconds": float(seconds),
        "final": final,
    }


# --- Before launching: an estimate ---------------------------------------------------------


def estimate(action: Action) -> dict[str, Any] | None:
    """What a run of the action may cost, on the template it would run on: typically, as its
    recent runs did, and at most for its timeout. None where runs cost nothing here."""
    if action.tier not in PRICED_TIERS:
        return None
    templates = catalog.availability(action).templates
    priced = next(((t, price(t.name)) for t in templates if price(t.name) is not None), None)
    if priced is None:
        return None
    template, costs = priced
    assert costs is not None  # noqa: S101 - found priced
    totals = [
        Decimal(row["total"])
        for row in CostRecord.objects.filter(
            run__action=action.qualified_name,
            run__status=RunStatus.SUCCEEDED,
            currency=costs.currency,
        )
        .values("run")
        .annotate(total=Sum("amount"), last=F("run__finished_at"))
        .order_by("-last")[:_ESTIMATE_FROM]
    ]
    typical = statistics.median(totals) if totals else None
    return {
        "template": template.name,
        "price_per_hour": float(costs.per_hour),
        "currency": costs.currency,
        "typical": float(typical.quantize(Decimal("0.01"))) if typical is not None else None,
        "runs_seen": len(totals),
        "timeout_cost": float(costs.of(Decimal(action.timeout)).quantize(Decimal("0.01"))),
    }


# --- Reports ---------------------------------------------------------------------------------

#: What a summary groups records by, and the field that names each group.
GROUPS = {
    "project": ("project", "project__name"),
    "team": ("team", "team__name"),
    "template": ("template", "template"),
    "action": ("action", "action"),
    "tier": ("tier", "tier"),
}


def between(since: datetime, until: datetime) -> QuerySet[CostRecord]:
    """Records of costs that ended in the period: a machine's cost counts when it's recorded."""
    return CostRecord.objects.filter(ended_at__gte=since, ended_at__lt=until)


def summary(records: QuerySet[CostRecord], by: str) -> dict[str, Any]:
    """Totals, and totals by group, in each currency the records are in. The organization's
    idle time is a group of its own when grouping by project or team."""
    key, label = GROUPS[by]
    totals = [
        {
            "currency": r["currency"] or None,
            "amount": float(r["amount"] or 0),
            "seconds": float(r["seconds"]),
        }
        for r in records.values("currency")
        .annotate(amount=Sum("amount"), seconds=Sum("seconds"))
        .order_by("currency")
    ]
    rows = (
        records.values(key, label, "currency")
        .annotate(
            amount=Sum("amount"),
            seconds=Sum("seconds"),
            runs=Count("run", distinct=True, filter=Q(kind=CostKind.RUN)),
        )
        .order_by(F("amount").desc(nulls_last=True), label)
    )
    groups = [
        {
            "key": str(row[key]) if row[key] is not None else None,
            "label": row[label] if row[key] is not None else "Idle machines",
            "currency": row["currency"] or None,
            "amount": float(row["amount"] or 0),
            "seconds": float(row["seconds"]),
            "runs": row["runs"],
        }
        for row in rows
    ]
    return {"totals": totals, "groups": groups}
