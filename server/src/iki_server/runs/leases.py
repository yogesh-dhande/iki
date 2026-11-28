"""What a worker's lease means, alike for a run's job (ADR 0043) and a setup build (ADR 0075).

A worker holds what it leased only while it sends heartbeats; the server ends what it
stopped renewing, and what ran well past its timeout. Its log is appended in order, up to
a limit. The queue and setup builds share these, so the two can't drift apart.
"""

from datetime import timedelta

__all__ = ["HEARTBEAT_EVERY", "LEASE_TTL", "MAX_LOG_BYTES", "TIMEOUT_GRACE", "LogGap", "Stale"]

#: A lease lasts this long unless a heartbeat renews it (a Slurm job's longer: ADR 0133).
LEASE_TTL = timedelta(seconds=60)
#: How often workers should send heartbeats (they're told when they lease).
HEARTBEAT_EVERY = timedelta(seconds=20)
#: How long past its timeout a job or build may run before the server ends it, if its
#: worker didn't.
TIMEOUT_GRACE = timedelta(minutes=5)
#: The most log one job or build keeps; past this, chunks are dropped and the log says so.
MAX_LOG_BYTES = 16 * 1024**2


class Stale(Exception):  # noqa: N818 - reads as "the lease is stale"
    """The worker doesn't hold the job's or build's current lease."""


class LogGap(Exception):  # noqa: N818 - reads as "there's a gap in the log"
    """A log chunk didn't start where the log ends."""

    def __init__(self, expected: int) -> None:
        super().__init__(f"The log continues at byte {expected}.")
        self.expected = expected
