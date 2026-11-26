"""Store movement notifications according to the webhook's scope, including during rollout."""

from django.db import migrations

SCOPE_MOVEMENTS = """
CREATE FUNCTION iki_webhook_movement_scope() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    project uuid;
BEGIN
    EXECUTE format('SELECT project_id FROM %I.iki_webhook WHERE id = $1', TG_TABLE_SCHEMA)
        INTO project USING NEW.webhook_id;
    IF project IS NULL THEN
        NEW.payload := jsonb_set(NEW.payload, '{data}', jsonb_build_object(
            'entity', NEW.payload #> '{data,entity}',
            'kind',   NEW.payload #> '{data,kind}',
            'actor',  NEW.payload #> '{data,actor}',
            'agent',  NEW.payload #> '{data,agent}'
        ));
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER iki_webhook_movement_scope
    BEFORE INSERT OR UPDATE OF payload, event, webhook_id ON iki_webhook_delivery
    FOR EACH ROW WHEN (NEW.event IN ('storage.moved', 'entity.amount_moved'))
    EXECUTE FUNCTION iki_webhook_movement_scope();
"""


class Migration(migrations.Migration):
    dependencies = [("iki_webhooks", "0002_app_grants")]

    operations = [
        migrations.RunSQL(
            SCOPE_MOVEMENTS,
            reverse_sql="""
            DROP TRIGGER iki_webhook_movement_scope ON iki_webhook_delivery;
            DROP FUNCTION iki_webhook_movement_scope();
            """,
        ),
        # Apply the same trigger to existing deliveries, which can be read, retried or resent.
        migrations.RunSQL(
            """
            UPDATE iki_webhook_delivery AS d SET payload = d.payload
            FROM iki_webhook AS w
            WHERE w.id = d.webhook_id AND w.project_id IS NULL
              AND d.event IN ('storage.moved', 'entity.amount_moved')
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
