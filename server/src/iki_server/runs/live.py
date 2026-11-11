"""Live updates about a run, for the pages following it (``GET /api/v1/runs/{ref}/events``)
and its batch's.

As with every live update, they say *that* something changed, never what: the page reads
the run, its attempts and its log through the API. Postgres delivers each one when its
transaction commits, so work that was rolled back says nothing.
"""

import uuid

from iki_server.realtime import events


def topic(run_id: uuid.UUID) -> str:
    return f"run:{run_id}"


def batch_topic(batch_id: uuid.UUID) -> str:
    return f"batch:{batch_id}"


def changed(run_id: uuid.UUID, batch_id: uuid.UUID | None = None) -> None:
    """Its status, an attempt's, or progress. With `batch_id` (when its status changed),
    the batch's page hears too (``GET /api/v1/batches/{ref}/events``)."""
    events.publish_to(topic(run_id), "run", {"id": str(run_id)})
    if batch_id is not None:
        events.publish_to(batch_topic(batch_id), "run", {"id": str(run_id)})


def logged(run_id: uuid.UUID, attempt: int, size: int) -> None:
    """Attempt number `attempt`'s log grew to `size` bytes."""
    events.publish_to(topic(run_id), "log", {"attempt": attempt, "size": size})
