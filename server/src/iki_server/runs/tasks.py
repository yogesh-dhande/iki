"""The runs' background tasks."""

from iki_server.runs import imports
from iki_server.tasks import task


@task(max_attempts=imports.MAX_FAILURES + 1)
def import_creates(import_id: str) -> str:
    """Works on a bulk import (ADR 0056) for a while; enqueues itself again until it ends.
    Safe to run twice: only one task works on an import at a time."""
    return imports.step(import_id)


@task(max_attempts=5)
def clear_import(import_id: str) -> str:
    """Deletes an import's staged lines once they're done with, a batch at a time."""
    return imports.clear(import_id)


@task(max_attempts=5)
def delete_checkpoints(run_id: str) -> int:
    """Safe to run twice: deleted checkpoints are left alone (ADR 0069)."""
    from iki_server.runs import checkpoints

    return checkpoints.delete_for(run_id)


@task(max_attempts=5)
def fire_trigger(firing_id: str) -> str:
    """Launches the run a change that fired a project's trigger asks for (ADR 0080). Safe
    to run twice: it locks the firing, and fires only one that hasn't been."""
    from iki_server.runs import triggers

    return triggers.fire(firing_id)


@task(max_attempts=5)
def advance_workflow(workflow_id: str) -> str:
    """Moves a workflow run on once one of its runs has ended (ADR 0079). Safe to run twice:
    it locks the workflow run, and only launches steps that haven't been."""
    from iki_server.runs import workflows

    return workflows.advance(workflow_id)
