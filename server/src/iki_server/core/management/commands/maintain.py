"""``iki-server maintain``: routine cleanup. The Helm chart runs it as a CronJob.

- Deletes expired sessions (they are already ignored; this keeps the table small).
- Prunes rate-limit windows older than an hour.
- Deletes expired OAuth tokens, codes and device grants.
- Deletes self-registered OAuth apps nobody asked to approve within a week.
"""

from typing import Any

import structlog
from django.core.management.base import BaseCommand
from django.utils import timezone

from iki_server.accounts.models import UserSession
from iki_server.oauth import maintenance as oauth_maintenance
from iki_server.web import ratelimit

logger = structlog.get_logger("iki.maintenance")


class Command(BaseCommand):
    help = "Delete expired sessions, tokens and rate-limit counters."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        sessions, _ = UserSession.objects.filter(expire_date__lt=timezone.now()).delete()
        counters = ratelimit.prune()
        apps = oauth_maintenance.prune()
        logger.info(
            "maintenance_done",
            expired_sessions=sessions,
            rate_limit_windows=counters,
            abandoned_oauth_apps=apps,
        )
        self.stdout.write(
            f"Deleted {sessions} expired session(s), {counters} old rate-limit window(s) "
            f"and {apps} abandoned app registration(s)."
        )
