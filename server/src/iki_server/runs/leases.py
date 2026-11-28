"""What a worker's lease means, alike for a run's job (ADR 0043) and a setup build (ADR 0075).

A worker holds what it leased only while it sends heartbeats; the server ends what it
stopped renewing, and what ran well past its timeout. Its log is appended in order, up to
a limit. The queue and setup builds share these rules, and the helpers below that carry
them out, so the two can't drift apart. How each is leased, and how it ends, stay with its
own module: a build is tied to the one machine started for it, a job is picked from a
queue, packed and retried.
"""

import uuid
from collections.abc import Callable, Collection
from datetime import datetime, timedelta

from django.db.models import DateTimeField, ExpressionWrapper, F, QuerySet
from django.db.models.expressions import Combinable
from django.utils import timezone

from iki_server.core import sweeps
from iki_server.runs.models import Job, SetupBuild

__all__ = [
    "HEARTBEAT_EVERY",
    "LEASE_TTL",
    "MAX_LOG_BYTES",
    "RECOVER_AT_MOST",
    "TIMEOUT_GRACE",
    "LogGap",
    "Stale",
    "append_log",
    "end_each",
    "fenced",
    "lost",
    "overdue",
    "set_progress",
]

#: A lease lasts this long unless a heartbeat renews it (a Slurm job's longer: ADR 0133).
LEASE_TTL = timedelta(seconds=60)
#: How often workers should send heartbeats (they're told when they lease).
HEARTBEAT_EVERY = timedelta(seconds=20)
#: How long past its timeout a job or build may run before the server ends it, if its
#: worker didn't.
TIMEOUT_GRACE = timedelta(minutes=5)
#: The most log one job or build keeps; past this, chunks are dropped and the log says so.
MAX_LOG_BYTES = 16 * 1024**2
#: The most one recovery pass ends in each sweep; the rest are due still, and the next pass
#: a few seconds later finds them first.
RECOVER_AT_MOST = 500


class Stale(Exception):  # noqa: N818 - reads as "the lease is stale"
    """The worker doesn't hold the job's or build's current lease."""


class LogGap(Exception):  # noqa: N818 - reads as "there's a gap in the log"
    """A log chunk didn't start where the log ends."""

    def __init__(self, expected: int) -> None:
        super().__init__(f"The log continues at byte {expected}.")
        self.expected = expected


# --- A worker's reports ----------------------------------------------------------------


def fenced[L: (Job, SetupBuild)](found: L | None, token: uuid.UUID, holding: Collection[str]) -> L:
    """`found`, if the worker reporting on it holds it by `token`: in one of the `holding`
    states, with that lease token, unexpired. Raises :class:`Stale` otherwise: it ended,
    or its lease ran out and another worker may hold it now. Every report a worker sends
    passes here, so a worker presumed lost can't change what replaced it."""
    if found is None or found.state not in holding or found.lease_token != token:
        raise Stale
    if found.lease_expires_at is None or found.lease_expires_at <= timezone.now():
        raise Stale
    return found


def set_progress[L: (Job, SetupBuild)](held: L, fraction: float, message: str) -> None:
    """Saves how far along the worker says it is, between 0 and 1, and what it's doing."""
    held.progress = max(0.0, min(1.0, fraction))
    held.progress_message = message[:500]
    held.save(update_fields=["progress", "progress_message"])


def append_log[L: (Job, SetupBuild)](
    held: L, offset: int, text: str, keep: Callable[[str], object]
) -> bool:
    """Adds a chunk at `offset` (in bytes of UTF-8) to the log of what the worker holds,
    locked, and says whether it was new. ``held.log_size`` is then where the next one goes.

    A chunk already received (the worker retried) is accepted again, unchanged; a gap or an
    overlap raises :class:`LogGap`, so the log stays exactly what the command wrote. Past
    ``MAX_LOG_BYTES`` chunks are counted, not kept, and the log says so. `keep` stores the
    text to keep at `offset`."""
    size = len(text.encode())
    end = held.log_size
    if offset < end and offset + size <= end:
        return False
    if offset != end:
        raise LogGap(end)
    held.log_size = end + size
    held.save(update_fields=["log_size"])
    if end >= MAX_LOG_BYTES:
        return True
    if end + size > MAX_LOG_BYTES:
        text = text.encode()[: MAX_LOG_BYTES - end].decode(errors="ignore")
        text += f"\n[iki: the log is over {MAX_LOG_BYTES // 1024**2} MiB; the rest isn't kept]\n"
    # Postgres text can't hold NUL.
    keep(text.replace("\x00", "�"))
    return True


# --- Recovering what was left behind -----------------------------------------------------


def lost[L: (Job, SetupBuild)](held: QuerySet[L], now: datetime) -> QuerySet[L]:
    """Of `held`, those whose lease ran out (their worker stopped sending heartbeats),
    longest gone first."""
    return held.filter(lease_expires_at__lt=now).order_by("lease_expires_at")


def overdue[L: (Job, SetupBuild)](
    held: QuerySet[L], began: Combinable, timeout: str, now: datetime
) -> QuerySet[L]:
    """Of `held`, those more than ``TIMEOUT_GRACE`` past their deadline, longest overdue
    first. The deadline is computed in the database, `began` plus the `timeout` field's
    seconds, so however many are held within theirs, the ones past it are found."""
    due = ExpressionWrapper(began + F(timeout) * timedelta(seconds=1), output_field=DateTimeField())
    return held.alias(due=due).filter(due__lt=now - TIMEOUT_GRACE).order_by("due")


def end_each[L: (Job, SetupBuild)](
    due: QuerySet[L], end: Callable[[L], object], *, failed: str, key: str
) -> int:
    """Ends each of `due`'s first ``RECOVER_AT_MOST`` in a transaction of its own
    (:func:`sweeps.each`): locked, skipping one another runner holds, and still one of
    `due` when it is. One that can't be ended is logged as `failed` and left for the next
    pass. Returns how many it ended."""
    ids = list(due.values_list("pk", flat=True)[:RECOVER_AT_MOST])

    def end_one(pk: uuid.UUID) -> bool:
        found = due.select_for_update(skip_locked=True, of=("self",)).filter(pk=pk).first()
        if found is None:
            return False
        end(found)
        return True

    return sweeps.each(ids, end_one, failed=failed, key=key)
