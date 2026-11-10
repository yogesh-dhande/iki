"""Housekeeping for the task queue, run by ``iki-server maintain``."""

from datetime import timedelta

from django.db.models import Q
from django.tasks import TaskResultStatus
from django.utils import timezone

from iki_server.tasks.models import TaskRecord

#: Finished tasks are kept this long, for troubleshooting. Failures stay longer.
KEEP_SUCCEEDED = timedelta(days=7)
KEEP_FAILED = timedelta(days=30)


def prune() -> int:
    """Deletes finished tasks past their keep time. Returns how many."""
    now = timezone.now()
    deleted, _ = TaskRecord.objects.filter(
        Q(status=TaskResultStatus.SUCCESSFUL, finished_at__lt=now - KEEP_SUCCEEDED)
        | Q(status=TaskResultStatus.FAILED, finished_at__lt=now - KEEP_FAILED)
    ).delete()
    return deleted
