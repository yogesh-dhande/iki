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
