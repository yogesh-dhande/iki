from iki_server.spreadsheets import services
from iki_server.tasks import task


@task(max_attempts=3)
def read_workbook(import_id: str) -> str:
    """Reads a workbook's sheets and their first rows (ADR 0178). Safe to run twice."""
    return services.read(import_id)


@task(max_attempts=services.MAX_FAILURES + 1)
def preview_import(import_id: str, round_: int) -> str:
    """Works on a preview for a while; enqueues itself again until it's done. Only one task
    works on a preview at a time."""
    return services.step(import_id, round_)


@task(max_attempts=5)
def commit_import(import_id: str) -> str:
    """Writes a previewed import's rows as its run's creates file, then starts the run."""
    return services.committing(import_id)


@task(max_attempts=5)
def clear_rows(import_id: str, round_: int) -> str:
    """Deletes a preview's rows once they're done with, a batch at a time."""
    return services.clear_rows(import_id, round_)
