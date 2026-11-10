"""Checking finished uploads in the background: reading a large file back takes a while."""

from iki_server.files import services
from iki_server.tasks import task


@task(max_attempts=5)
def check_upload(upload_id: str) -> str:
    """Safe to run twice: an upload that's no longer being checked is left alone."""
    return services.check(upload_id)
