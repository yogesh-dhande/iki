"""Remember record locks across state changes and declaration migrations (ADR 0126)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0023_ruleholdoccurrence")]
    operations = [
        migrations.AddField(
            model_name="entity",
            name="has_been_locked",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.RunSQL(
            """
            UPDATE iki_entity SET has_been_locked = true WHERE cardinality(locked_by) > 0;
            CREATE FUNCTION iki_remember_record_lock() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                NEW.has_been_locked := NEW.has_been_locked OR cardinality(NEW.locked_by) > 0;
                IF TG_OP = 'UPDATE' THEN
                    NEW.has_been_locked := NEW.has_been_locked OR OLD.has_been_locked;
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER iki_remember_record_lock
                BEFORE INSERT OR UPDATE OF locked_by, has_been_locked ON iki_entity
                FOR EACH ROW EXECUTE FUNCTION iki_remember_record_lock();
            """,
            """
            DROP TRIGGER iki_remember_record_lock ON iki_entity;
            DROP FUNCTION iki_remember_record_lock();
            """,
        ),
    ]
