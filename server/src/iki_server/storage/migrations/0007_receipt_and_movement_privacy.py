"""Bring stored discrepancy notices and project webhook deliveries under the same read rules."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("iki_storage", "0006_count_observation_reviews"),
        ("iki_notifications", "0002_notifications_about_records"),
        ("iki_webhooks", "0002_app_grants"),
    ]

    operations = [
        # The title already holds the tally. Open the request’s filtered receipt history.
        migrations.RunSQL(
            """
            UPDATE iki_notification
            SET body = '', path = CASE WHEN entity_id IS NULL THEN path
                                       ELSE '/storage/receive/' || entity_id || '/' END
            WHERE kind = 'storage.receipt_discrepancy'
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # A saved delivery can still be read, retried or sent again.
        migrations.RunSQL(
            [
                (
                    """
                    UPDATE iki_webhook_delivery AS d
                    SET payload = jsonb_set(d.payload, ARRAY['data', %s], 'null')
                    FROM iki_webhook AS w
                    WHERE d.webhook_id = w.id AND w.project_id IS NOT NULL
                      AND d.event = 'storage.moved'
                      AND d.payload #>> ARRAY['data', %s] IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM iki_entity_project AS link
                          JOIN iki_entity AS e ON e.id = link.entity_id
                          WHERE link.project_id = w.project_id AND e.purged_at IS NULL
                            AND e.id = (d.payload #>> ARRAY['data', %s, 'container'])::uuid
                      )
                    """,
                    [endpoint, endpoint, endpoint],
                )
                for endpoint in ("from", "to")
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
