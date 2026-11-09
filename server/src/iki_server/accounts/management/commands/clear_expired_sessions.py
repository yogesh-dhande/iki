"""``iki-server clear_expired_sessions``: deletes expired sessions.

Expired sessions are already ignored; this keeps the table small. The Helm
chart runs it daily as a CronJob.
"""

from typing import Any

import structlog
from django.core.management.base import BaseCommand
from django.utils import timezone

from iki_server.accounts.models import UserSession

logger = structlog.get_logger("iki.accounts")


class Command(BaseCommand):
    help = "Delete expired sessions."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        deleted, _ = UserSession.objects.filter(expire_date__lt=timezone.now()).delete()
        logger.info("expired_sessions_cleared", count=deleted)
        self.stdout.write(f"Deleted {deleted} expired session(s).")
