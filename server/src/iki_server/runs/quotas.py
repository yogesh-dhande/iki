"""Limits on runs that Compute admins set (ADR 0045).

A quota covers the organization, a team or a project, on one tier or all of them, and
sets either limit or both:

- ``max_running``: jobs held by workers at once. Past it, jobs in that scope wait even
  when a worker is free; 0 pauses the scope.
- ``max_queued``: runs waiting to start. Launching more is refused.

Every quota that covers a job applies, so a project's own limit and its team's both
hold. Checks take an advisory lock, but only while quotas exist for that tier, so two
workers leasing at once can't both take the last slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from django.db import connection
from django.db.models import Q, QuerySet

from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun, Job, JobState, RunQuota, RunStatus

__all__ = ["Blocked", "blocked", "check_queued"]

#: States in which a worker holds a job (as the queue's HELD).
_HELD = (JobState.LEASED, JobState.STAGING, JobState.RUNNING, JobState.UPLOADING)


@dataclass
class Blocked:
    """Scopes at their running limit on a tier, and why, in words for people."""

    organization: str | None = None
    teams: dict[UUID, str] = field(default_factory=dict)
    projects: dict[UUID, str] = field(default_factory=dict)

    def why(self, project: Project) -> str | None:
        return self.projects.get(project.pk) or self.teams.get(project.team_id) or self.organization


def _covering(tier: str) -> QuerySet[RunQuota]:
    return RunQuota.objects.filter(Q(tier=tier) | Q(tier__isnull=True))


def _lock(name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [name])


def _scope_label(quota: RunQuota) -> str:
    if quota.project_id is not None:
        return "The project"
    if quota.team_id is not None:
        return "Its team"
    return "The organization"


def _tier_words(quota: RunQuota) -> str:
    return f" on the {quota.tier} tier" if quota.tier else ""


def blocked(tier: str, *, lock: bool = False) -> Blocked:
    """Which scopes are at their running limit on `tier`, or whose enforced budget is spent
    (ADR 0071). With `lock`, call inside the transaction that leases: the lock is held until
    it ends."""
    from iki_server.runs import budgets

    quotas = list(_covering(tier).filter(max_running__isnull=False))
    out = Blocked()
    budgets.hold(out, tier)
    if not quotas:
        return out
    if lock:
        _lock(f"iki_quota_running:{tier}")
    for quota in quotas:
        # A packed job counts once: it's one command on one worker (ADR 0055).
        held = Job.objects.filter(state__in=_HELD, packed_into__isnull=True)
        if quota.tier:
            held = held.filter(tier=quota.tier)
        if quota.project_id is not None:
            held = held.filter(project_id=quota.project_id)
        elif quota.team_id is not None:
            held = held.filter(project__team_id=quota.team_id)
        assert quota.max_running is not None  # noqa: S101 - filtered above
        if held.count() < quota.max_running:
            continue
        why = (
            f"{_scope_label(quota)} is paused{_tier_words(quota)}."
            if quota.max_running == 0
            else f"{_scope_label(quota)} is at its limit of {quota.max_running} running "
            f"job{'s' if quota.max_running != 1 else ''}{_tier_words(quota)}."
        )
        if quota.project_id is not None:
            out.projects.setdefault(quota.project_id, why)
        elif quota.team_id is not None:
            out.teams.setdefault(quota.team_id, why)
        else:
            out.organization = out.organization or why
    return out


def check_queued(project: Project, tier: str) -> None:
    """Refuses a launch past a waiting-runs limit. Call inside the launching transaction."""
    quotas = list(
        _covering(tier)
        .filter(max_queued__isnull=False)
        .filter(
            Q(project=project)
            | Q(team_id=project.team_id)
            | Q(project__isnull=True, team__isnull=True)
        )
    )
    if not quotas:
        return
    _lock(f"iki_quota_queued:{tier}")
    for quota in quotas:
        waiting = ActionRun.objects.filter(status=RunStatus.QUEUED)
        if quota.tier:
            waiting = waiting.filter(tier=quota.tier)
        if quota.project_id is not None:
            waiting = waiting.filter(project_id=quota.project_id)
        elif quota.team_id is not None:
            waiting = waiting.filter(project__team_id=quota.team_id)
        assert quota.max_queued is not None  # noqa: S101 - filtered above
        count = waiting.count()
        if count >= quota.max_queued:
            raise Refused(
                f"{_scope_label(quota)} has {count} run{'s' if count != 1 else ''} waiting to "
                f"start{_tier_words(quota)}, its limit. Launch this once some have started, "
                "or ask a Compute admin to raise it."
            )
