"""``iki-server maintain``: routine cleanup. The Helm chart runs it as a CronJob.

- Deletes expired sessions (they are already ignored; this keeps the table small).
- Prunes rate-limit windows older than an hour.
- Deletes expired OAuth tokens, codes and device grants.
- Deletes self-registered OAuth apps nobody asked to approve within a week.
- Deletes finished background tasks after a week (failed ones after 30 days).
- Cancels uploads left unfinished for a day, deleting what of them arrived, and fails
  registrations still being read after a day.
- Expires requests for runs that agents asked for and nobody approved within a week.
- Deletes runs' artifacts whose retention is over (ADR 0068), every version of them.
- Deletes checkpoints no attempt will resume from (ADR 0069): those of ended runs, and those
  a newer one replaced, in case the task that deletes them didn't.
- Deletes apps' drafts (ADR 0090) once they expire, a day after they're made.
- Deletes exported bundles' archives (ADR 0096) a week after they're written, except in
  projects under a legal hold.
- Discards spreadsheet imports (ADR 0178) nobody committed or touched for a fortnight,
  and the rows their previews kept.
- Notifies project stock managers of catalogue items at or below their reorder threshold.
"""

from typing import Any

import structlog
from django.core.management.base import BaseCommand
from django.utils import timezone

from iki_server.accounts.models import UserSession
from iki_server.amounts import stock_alerts
from iki_server.bundles import exports as bundle_exports
from iki_server.entities import app_drafts
from iki_server.files import scan_recovery
from iki_server.files import services as files
from iki_server.oauth import maintenance as oauth_maintenance
from iki_server.plugins import purge
from iki_server.runs import approvals, artifacts, checkpoints, queue
from iki_server.spreadsheets import services as spreadsheets
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
        lapsed_webhooks = webhook_maintenance.lapse()
        uploads = files.expire()
        registrations = files.expire_registrations()
        scans = scan_recovery.recover()
        approvals_expired = approvals.expire()
        artifacts_deleted = artifacts.delete_expired()
        checkpoints_deleted = checkpoints.delete_stale()
        drafts = app_drafts.prune()
        bundles = bundle_exports.expire()
        detached = queue.end_detached()
        tombstoned = purge.tombstone_due()
        sheets = spreadsheets.expire()
        alerts = stock_alerts.send_alerts()
        logger.info(
            "maintenance_done",
            expired_sessions=sessions,
            rate_limit_windows=counters,
            abandoned_oauth_apps=apps,
            finished_tasks=tasks,
            old_webhook_deliveries=deliveries,
            lapsed_app_webhooks=lapsed_webhooks,
            queued_file_scans=scans,
            abandoned_uploads=uploads,
            stalled_registrations=registrations,
            expired_run_approvals=approvals_expired,
            deleted_artifacts=artifacts_deleted,
            deleted_checkpoints=checkpoints_deleted,
            expired_app_drafts=drafts,
            expired_bundles=bundles,
            ended_detached_runs=detached,
            tombstoned_entities=tombstoned,
            abandoned_spreadsheet_imports=sheets,
            low_stock_notifications=alerts,
        )
        self.stdout.write(
            f"Deleted {sessions} expired session(s), {counters} old rate-limit window(s), "
            f"{apps} abandoned app registration(s), {tasks} finished task(s) and "
            f"{deliveries} old webhook deliveries, turned off {lapsed_webhooks} webhook(s) "
            f"apps added and may no longer, cancelled {uploads} abandoned upload(s), "
            f"failed {registrations} stalled registration(s), expired "
            f"{approvals_expired} run approval request(s), deleted {artifacts_deleted} "
            f"run artifact(s) whose retention was over, deleted {checkpoints_deleted} "
            f"run checkpoint(s) nothing will resume from, deleted {drafts} expired app "
            f"draft(s), deleted {bundles} expired bundle(s), discarded {sheets} abandoned "
            f"spreadsheet import(s), ended {detached} run(s) of detached plugins, and tombstoned "
            f"{tombstoned} entit{'y' if tombstoned == 1 else 'ies'} of purged plugins. "
            f"Sent {alerts} low-stock notification(s) and queued {scans} outstanding file scan(s)."
        )
