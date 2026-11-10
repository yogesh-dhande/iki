"""``iki-server maintain``: routine cleanup. The Helm chart runs it as a CronJob.

- Deletes expired sessions (they are already ignored; this keeps the table small).
- Prunes rate-limit windows older than an hour.
"""

from typing import Any

import structlog
from django.core.management.base import BaseCommand
from django.utils import timezone

from iki_server.accounts.models import UserSession
from iki_server.web import ratelimit

logger = structlog.get_logger("iki.maintenance")


class Command(BaseCommand):
    help = "Delete expired sessions and old rate-limit counters."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        sessions, _ = UserSession.objects.filter(expire_date__lt=timezone.now()).delete()
        counters = ratelimit.prune()
        logger.info("maintenance_done", expired_sessions=sessions, rate_limit_windows=counters)
        self.stdout.write(
            f"Deleted {sessions} expired session(s) and {counters} old rate-limit window(s)."
        )
