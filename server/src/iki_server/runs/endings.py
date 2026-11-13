"""What follows when a run ends, however it ends: finished by its worker, failed, timed
out, cancelled or declined."""

from iki_server.runs import checkpoints
from iki_server.runs.models import ActionRun


def ended(run: ActionRun) -> None:
    """The run ended. Call inside the transaction that ended it: what follows is queued in
    it, so it happens exactly when the run has ended."""
    checkpoints.ended(run)
    if run.workflow_id is not None:
        # Its workflow moves on (ADR 0079).
        from iki_server.runs.tasks import advance_workflow

        advance_workflow.enqueue(str(run.workflow_id))
