"""Runs agents ask for that wait for a person's approval (ADR 0013).

An agent acting for someone through MCP may start runs on the client and backend
tiers itself. On the container and VM tiers, which cost real money, its runs wait,
with their jobs held where no worker leases them, until that person approves them in
the web app. Approving is refused to any OAuth token, so an agent can't approve its
own request. One request is one decision: a batch is approved or declined together,
and a request nobody decides within a week expires.

Approving checks again that the person may run the tier in the project, and the
quotas on waiting runs; the jobs then join the queue as if just launched.
"""

from datetime import timedelta
from functools import partial
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import Via
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import live, queue, quotas
from iki_server.runs.models import (
    ActionRun,
    ApprovalState,
    Job,
    JobState,
    RunApproval,
    RunStatus,
)
from iki_spec.model import Action

#: Tiers whose runs an agent can't start by itself.
APPROVAL_TIERS = frozenset({"container", "vm"})
#: How long a request waits for a decision.
EXPIRES_AFTER = timedelta(days=7)


def needed(action: Action) -> bool:
    """Whether a run of `action`, launched now, waits for approval."""
    return audit.current_via() == Via.MCP and action.tier in APPROVAL_TIERS


def ask(user: User, action: Action, project: Project, *, runs: int = 1) -> RunApproval | None:
    """The approval a launch's runs wait for, if they need one. Call inside the launch's
    transaction, then :func:`announce` once its runs exist."""
    if not needed(action):
        return None
    approval = RunApproval.objects.create(
        project=project,
        requested_by=user,
        client=audit.current_oauth_client(),
        expires_at=timezone.now() + EXPIRES_AFTER,
    )
    audit.record(
        "run.approval_requested",
        actor=user,
        target=approval,
        project=project,
        data={"action": action.qualified_name, "tier": action.tier, "runs": runs},
    )
    return approval


def announce(approval: RunApproval, runs: int, label: str, *, path: str) -> None:
    """Tells the person their agent is asking, with a link to decide."""
    what = f"{runs} runs of {label}" if runs > 1 else f"a run of {label}"
    notify(
        [approval.requested_by],
        kinds.RUN_APPROVAL_REQUESTED,
        title=f"{approval.client or 'An app'} asks to start {what} in {approval.project.name}",
        body="It runs on a tier that needs your approval. Approve or decline it in Iki; "
        f"it expires in {EXPIRES_AFTER.days} days.",
        path=path,
        project=approval.project,
    )


def _may_decide(user: User, approval: RunApproval) -> None:
    # Tokens can't, whatever their scopes: else an agent could approve its own request.
    if permissions.token_limits(user) is not None:
        raise PermissionDenied
    if approval.requested_by_id != user.pk:
        raise PermissionDenied


def _pending(approval: RunApproval) -> RunApproval:
    """The approval, locked, if it still waits for a decision; else raises why not. Call
    inside a transaction, after :func:`_expire_if_due`."""
    locked = RunApproval.objects.select_for_update().get(pk=approval.pk)
    if locked.state != ApprovalState.PENDING:
        raise Refused(f"This request is {locked.get_state_display().lower()} already.")
    return locked


def _expire_if_due(approval: RunApproval) -> None:
    """Expires it if its time is up, in a transaction of its own, so it stays expired."""
    with transaction.atomic():
        locked = RunApproval.objects.select_for_update().get(pk=approval.pk)
        if locked.state == ApprovalState.PENDING and locked.expires_at <= timezone.now():
            _end(locked, ApprovalState.EXPIRED, None)


def _waiting(approval: RunApproval) -> list[ActionRun]:
    # Jobs before their runs, as the queue locks them, so the two never deadlock.
    list(
        Job.objects.select_for_update().filter(
            run__approval=approval, state=JobState.AWAITING_APPROVAL
        )
    )
    return list(
        ActionRun.objects.select_for_update()
        .select_related("project")
        .filter(approval=approval, status=RunStatus.AWAITING_APPROVAL)
        .order_by("id")
    )


def approve(user: User, approval: RunApproval) -> list[ActionRun]:
    """Queues the runs `approval` holds, as the person who asked."""
    from iki_server.runs.services import TIER_PERMISSIONS

    _may_decide(user, approval)
    _expire_if_due(approval)
    with transaction.atomic():
        approval = _pending(approval)
        runs = _waiting(approval)
        for run in runs:
            if not permissions.can(user, TIER_PERMISSIONS[run.tier], run.project):
                raise PermissionDenied
            if run.project.archived_at is not None:
                raise Refused(f"{run.project.name} is archived, so nothing new can run in it.")
        now = timezone.now()
        for run in runs:
            quotas.check_queued(run.project, run.tier)
            run.status = RunStatus.QUEUED
            run.save(update_fields=["status"])
        Job.objects.filter(run__in=runs, state=JobState.AWAITING_APPROVAL).update(
            state=JobState.QUEUED, queued_at=now
        )
        approval.state = ApprovalState.APPROVED
        approval.decided_by = user
        approval.decided_at = now
        approval.save(update_fields=["state", "decided_by", "decided_at"])
        audit.record(
            "run.approved",
            actor=user,
            target=approval,
            project=approval.project,
            data={"runs": [r.hrid for r in runs]},
        )
    for tier in {r.tier for r in runs}:
        queue.notify(tier)
    for run in runs:
        live.changed(run.pk, run.batch_id)
    return runs


def decline(user: User, approval: RunApproval, *, note: str = "") -> list[ActionRun]:
    """Ends the runs `approval` holds, unstarted."""
    _may_decide(user, approval)
    _expire_if_due(approval)
    with transaction.atomic():
        approval = _pending(approval)
        return _end(approval, ApprovalState.DECLINED, user, note=note)


def _end(
    approval: RunApproval, state: ApprovalState, user: User | None, *, note: str = ""
) -> list[ActionRun]:
    """Declines or expires `approval`, ending its waiting runs. Call inside a transaction,
    with it locked."""
    runs = _waiting(approval)
    now = timezone.now()
    Job.objects.filter(run__in=runs, state=JobState.AWAITING_APPROVAL).update(
        state=JobState.CANCELLED, finished_at=now
    )
    ActionRun.objects.filter(pk__in=[r.pk for r in runs]).update(
        status=RunStatus.DECLINED, finished_at=now
    )
    approval.state = state
    approval.decided_by = user
    approval.decided_at = now
    approval.note = note.strip()
    approval.save(update_fields=["state", "decided_by", "decided_at", "note"])
    data: dict[str, Any] = {"runs": [r.hrid for r in runs]}
    if note.strip():
        data["note"] = note.strip()
    audit.record(
        "run.declined" if state == ApprovalState.DECLINED else "run.approval_expired",
        actor=user,
        target=approval,
        project=approval.project,
        data=data,
    )
    for run in runs:
        transaction.on_commit(partial(live.changed, run.pk, run.batch_id))
    return runs


def settle(approval: RunApproval, by: User) -> None:
    """After a run it holds was cancelled: with none left waiting, nothing is left to
    decide. Call inside the cancelling transaction."""
    if approval.state != ApprovalState.PENDING:
        return
    if ActionRun.objects.filter(approval=approval, status=RunStatus.AWAITING_APPROVAL).exists():
        return
    approval.state = ApprovalState.DECLINED
    approval.decided_by = by
    approval.decided_at = timezone.now()
    approval.note = "Its runs were cancelled."
    approval.save(update_fields=["state", "decided_by", "decided_at", "note"])


def expire() -> int:
    """Expires requests nobody decided in time. The maintenance job runs it."""
    expired = 0
    for pk in RunApproval.objects.filter(
        state=ApprovalState.PENDING, expires_at__lte=timezone.now()
    ).values_list("pk", flat=True):
        with transaction.atomic():
            approval = RunApproval.objects.select_for_update().get(pk=pk)
            if approval.state == ApprovalState.PENDING:
                _end(approval, ApprovalState.EXPIRED, None)
                expired += 1
    return expired
