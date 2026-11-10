"""``iki-server maintain``: routine cleanup. The Helm chart runs it as a CronJob.

- Deletes expired sessions (they are already ignored; this keeps the table small).
- Prunes rate-limit windows older than an hour.
- Deletes expired OAuth tokens, codes and device grants.
- Deletes self-registered OAuth apps nobody asked to approve within a week.
- Deletes finished background tasks after a week (failed ones after 30 days).
- Cancels uploads left unfinished for a day, deleting what of them arrived, and fails
  registrations still being read after a day.
"""

from typing import Any

import structlog
from django.core.management.base import BaseCommand
from django.utils import timezone

from iki_server.accounts.models import UserSession
from iki_server.files import services as files
from iki_server.oauth import maintenance as oauth_maintenance
from iki_server.tasks import maintenance as task_maintenance
from iki_server.web import ratelimit
from iki_server.webhooks import maintenance as webhook_maintenance

logger = structlog.get_logger("iki.maintenance")


class Command(BaseCommand):
    help = (
        "Delete expired sessions, tokens, rate-limit counters, finished tasks and old "
        "webhook deliveries, and cancel abandoned uploads."
    )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        sessions, _ = UserSession.objects.filter(expire_date__lt=timezone.now()).delete()
        counters = ratelimit.prune()
        apps = oauth_maintenance.prune()
        tasks = task_maintenance.prune()
        deliveries = webhook_maintenance.prune()
        uploads = files.expire()
        registrations = files.expire_registrations()
        logger.info(
            "maintenance_done",
            expired_sessions=sessions,
            rate_limit_windows=counters,
            abandoned_oauth_apps=apps,
            finished_tasks=tasks,
            old_webhook_deliveries=deliveries,
            abandoned_uploads=uploads,
            stalled_registrations=registrations,
        )
        self.stdout.write(
            f"Deleted {sessions} expired session(s), {counters} old rate-limit window(s), "
            f"{apps} abandoned app registration(s), {tasks} finished task(s) and "
            f"{deliveries} old webhook deliveries, cancelled {uploads} abandoned upload(s), "
            f"and failed {registrations} stalled registration(s)."
        )
